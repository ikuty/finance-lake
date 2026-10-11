#!/usr/bin/env python3
"""REIT（投資法人）の決算短信（PDF＋iXBRL）を、東証上場会社情報サービスの会社詳細
ページから一括で遡及取得する。

日次のTDnet監視（run_daily.py）とは別の、REIT専用のワンショットバックフィル。
JPXの会社詳細ページは1回のリクエストでその銘柄の決算短信履歴を丸ごと返す
（ページング不要、実機確認: 日本ビルファンド投資法人(8951)で2017-02-15〜現在の
20期分を1レスポンスで取得できた）ため、TDnetの日次イベントを1日ずつ遡る必要がない。
詳細はdocs/reit_support_design.md参照。

既にファイルが存在する場合はスキップする（SQLite進捗DBを使わず、レイクのファイルの
存在チェックだけで冪等性を持たせる。このリポジトリの他のスクリプトと同じ
「後段はレイクのファイルをglobするだけで完結させる」設計方針に合わせるため）。

Usage:
    python3 backfill_reit.py                  # seedの全REITを対象に実行
    python3 backfill_reit.py --sec-code 32690  # 単一銘柄のみ（検証用）

設定は環境変数から読む(Dockerの--env-fileを想定):
    DATA_DIR             省略時 /data/raw
    LOG_PATH             省略時 /data/logs/backfill-reit.log
    REIT_SEED_CSV_PATH   省略時 /app/seed/edinet_reit_issuers.csv
    REQUEST_DELAY        省略時 2.0 (秒)。run_daily.pyと同じ既定値
    TIME_BUDGET_SECONDS  省略時 5400 (90分)。電源枠を使い切らないための安全弁
                         （edinet-dl・run_daily.pyと同じ設計）
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path

import jpx_disclosure_client
from http_client import RetryingHttpClient
from run_daily import date_hierarchy_dir, save_atomic

DEFAULT_DATA_DIR = "/data/raw"
DEFAULT_LOG_PATH = "/data/logs/backfill-reit.log"
DEFAULT_SEED_CSV_PATH = "/app/seed/edinet_reit_issuers.csv"
DEFAULT_REQUEST_DELAY = 2.0
DEFAULT_TIME_BUDGET_SECONDS = 90 * 60
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5


@dataclass
class BackfillStats:
    companies_processed: int = 0
    companies_failed: dict[str, str] = field(default_factory=dict)
    kessan_found: int = 0
    kessan_downloaded: int = 0
    kessan_skipped_existing: int = 0
    kessan_failed: int = 0


def setup_logger(log_path: str) -> logging.Logger:
    logger = logging.getLogger("backfill-reit")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(log_path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    return logger


def load_reit_companies(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open(encoding="utf-8") as f:
        return [
            {
                "edinet_code": row["edinet_code"].strip(),
                "sec_code": row["sec_code"].strip(),
                "filer_name": row["filer_name"].strip(),
            }
            for row in csv.DictReader(f)
        ]


def build_metadata(
    company: dict[str, str], match: jpx_disclosure_client.JpxDisclosure
) -> dict[str, str | None]:
    """run_daily.build_pdf_metadataのREITバックフィル版。TDnetイベントを経由しない
    （tdnet_*フィールドが存在しない）ため、出自が分かるよう`source`を付与する。"""
    return {
        "edinet_code": company["edinet_code"],
        "sec_code": company["sec_code"],
        "company_name": company["filer_name"],
        "disclosure_kind": "kessan_tanshin",
        "jpx_disclosure_date": match.disclosure_date,
        "jpx_title": match.title,
        "pdf_url": match.pdf_url,
        "ixbrl_url": match.ixbrl_url,
        "source": "reit_backfill",
    }


def disclosure_date_to_iso(disclosure_date: str) -> str:
    """JPX表示形式(YYYY/MM/DD)をISO形式(YYYY-MM-DD)に変換する（date_hierarchy_dirが
    ISO形式を前提にしているため）。"""
    year, month, day = disclosure_date.split("/")
    return f"{year}-{month.zfill(2)}-{day.zfill(2)}"


def backfill_company(
    client: RetryingHttpClient,
    company: dict[str, str],
    data_dir: Path,
    delay: float,
    stats: BackfillStats,
    logger: logging.Logger,
) -> None:
    sec_code = company["sec_code"]
    edinet_code = company["edinet_code"]
    name = company["filer_name"]

    html = jpx_disclosure_client.fetch_company_page(client, sec_code)
    disclosures = jpx_disclosure_client.parse_kessan_disclosures(html)
    kessan = [d for d in disclosures if jpx_disclosure_client.title_matches_kind(d.title, "kessan_tanshin")]
    stats.kessan_found += len(kessan)
    logger.info(f"{edinet_code} {name} (sec_code={sec_code}): 決算短信{len(kessan)}件検出")

    for match in kessan:
        try:
            basename = match.pdf_url.rsplit("/", 1)[-1]
            event_date_iso = disclosure_date_to_iso(match.disclosure_date)
            dest = date_hierarchy_dir(data_dir, event_date_iso) / edinet_code / basename

            if dest.exists():
                stats.kessan_skipped_existing += 1
                continue

            body = jpx_disclosure_client.download_pdf(client, match.pdf_url)
            save_atomic(dest, body)

            if match.ixbrl_url:
                ixbrl_basename = match.ixbrl_url.rsplit("/", 1)[-1]
                ixbrl_body = jpx_disclosure_client.download_ixbrl(client, match.ixbrl_url)
                save_atomic(dest.parent / ixbrl_basename, ixbrl_body)

            metadata_json = json.dumps(build_metadata(company, match), ensure_ascii=False, indent=2).encode(
                "utf-8"
            )
            save_atomic(dest.with_suffix(".json"), metadata_json)

            stats.kessan_downloaded += 1
        except Exception as e:
            stats.kessan_failed += 1
            logger.error(f"{edinet_code} {name}: {match.disclosure_date} {match.title!r} 取得失敗 ({e})")
        finally:
            time.sleep(delay)


def run(
    companies: list[dict[str, str]],
    data_dir: Path,
    delay: float,
    time_budget_seconds: float,
    logger: logging.Logger,
) -> BackfillStats:
    stats = BackfillStats()
    client = RetryingHttpClient()
    run_started = time.monotonic()
    try:
        for company in companies:
            elapsed = time.monotonic() - run_started
            if elapsed >= time_budget_seconds:
                logger.info(
                    f"時間予算({time_budget_seconds:.0f}秒)に達したため残り"
                    f"{len(companies) - stats.companies_processed}社は次回以降に持ち越し"
                )
                break
            try:
                backfill_company(client, company, data_dir, delay, stats, logger)
                stats.companies_processed += 1
            except Exception as e:
                stats.companies_failed[company["edinet_code"]] = str(e)
                logger.error(f"{company['edinet_code']} {company['filer_name']}: 会社詳細ページ取得失敗 ({e})")
            time.sleep(delay)
    finally:
        client.close()
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sec-code", type=str, help="単一銘柄のみを対象にする（検証用）")
    args = parser.parse_args()

    data_dir = Path(os.environ.get("DATA_DIR", DEFAULT_DATA_DIR))
    log_path = os.environ.get("LOG_PATH", DEFAULT_LOG_PATH)
    seed_csv_path = Path(os.environ.get("REIT_SEED_CSV_PATH", DEFAULT_SEED_CSV_PATH))
    delay = float(os.environ.get("REQUEST_DELAY", DEFAULT_REQUEST_DELAY))
    time_budget_seconds = float(os.environ.get("TIME_BUDGET_SECONDS", DEFAULT_TIME_BUDGET_SECONDS))

    logger = setup_logger(log_path)
    data_dir.mkdir(parents=True, exist_ok=True)

    companies = load_reit_companies(seed_csv_path)
    if args.sec_code:
        companies = [c for c in companies if c["sec_code"] == args.sec_code]
        if not companies:
            logger.error(f"sec_code={args.sec_code}はseedに存在しません")
            sys.exit(1)

    logger.info(f"対象{len(companies)}社のREIT決算短信バックフィルを開始")
    stats = run(companies, data_dir, delay, time_budget_seconds, logger)
    logger.info(
        f"完了: 会社{stats.companies_processed}/{len(companies)}社処理 "
        f"(失敗{len(stats.companies_failed)}社) / "
        f"決算短信 検出{stats.kessan_found}件 "
        f"新規取得{stats.kessan_downloaded}件 "
        f"既存スキップ{stats.kessan_skipped_existing}件 "
        f"失敗{stats.kessan_failed}件"
    )
    if stats.companies_failed:
        for edinet_code, err in stats.companies_failed.items():
            logger.error(f"  {edinet_code}: {err}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""事業会社（普通株式発行体）の決算短信PDFを、東証上場会社情報サービスの会社詳細
ページから遡及取得する（backfill_reit.pyの事業会社版）。

背景: 日次のTDnet監視（run_daily.py）は2026-09-27稼働で、遡及バックフィルも直近90日分
（2026-07-02〜）だけ。DWHは決算短信の純資産から四半期・半期のBPSを推定してPBRを
埋める（finance-dwh intermediate__ir_disclosure__bps_estimates）ため、過去の決算短信が
無い期間（例: 任天堂 2025-11・2026-02・2026-05）はPBRがNULLのまま残る。JPXの会社詳細ページは
1回のリクエストでその銘柄の決算短信履歴を丸ごと返す（backfill_reit.pyの実機確認）ので、
TDnetを1日ずつ遡らずに過去分を取得できる。

backfill_reit.pyとの違い:
  - 対象はseed/edinet_common_stock_issuers.csv（普通株式発行体）
  - iXBRL（.htm）は既定では取得しない（事業会社のDWH取り込みはPDFのみを読む）。--with-ixbrl で取得
  - --since YYYY-MM-DD より前の開示は取得しない（既定 2024-03-01。取得量と所要時間の抑制）
  - メタデータJSONはrun_daily.build_pdf_metadataと同じキーに揃える（TDnet由来のtdnet_*は無いので省く）。
    出自が分かるよう source='tanshin_backfill' を付ける
  - 決算短信以外（業績予想の修正など）は対象にしない（title_matches_kindでkessan_tanshinのみ）

既にファイルが存在する場合はスキップする（レイクのファイルの存在チェックで冪等）。

Usage:
    python3 backfill_tanshin.py --sec-code 79740            # 単一銘柄（検証用）
    python3 backfill_tanshin.py --sec-code 79740 --dry-run  # 検出だけ（ダウンロードしない）
    python3 backfill_tanshin.py --since 2025-01-01          # seedの全銘柄

出力先はraw/ではなく**ステージング**（STAGING_DIR）。raw/に日付フォルダが出ると夜間の
transformが時間予算なしでバックログ取り込みを始めるため、完了した月だけを
promote_staging.py でraw/へ昇格する（finance-dwh Issue #94）。
会社ごとの進捗は STAGING_DIR/_progress.json に記録し、再実行時は完了済みの会社を飛ばす。

自動中断（終了コード3）:
  - レート制限の兆候: 429/403/503系のエラー、またはPDF以外の応答が連続、または応答が遅い状態が続く
  - 失敗率が高い（既定: 試行50件以上で5%超）

設定は環境変数から読む(Dockerの--env-fileを想定):
    STAGING_DIR          省略時 /data/staging/tanshin（出力先）
    RAW_DIR              省略時 /data/raw（取得済み判定のみに使う。書き込まない）
    LOG_PATH             省略時 /data/logs/backfill-tanshin.log
    SEED_CSV_PATH        省略時 /app/seed/edinet_common_stock_issuers.csv
    REQUEST_DELAY        省略時 1.0 (秒)
    TIME_BUDGET_SECONDS  省略時 19800 (5.5時間)。6時間の電源枠を使い切らないための安全弁
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections import deque
from pathlib import Path

import jpx_disclosure_client
from backfill_reit import (
    BackfillStats,
    disclosure_date_to_iso,
    load_reit_companies,
    setup_logger,
)
from http_client import RetryingHttpClient
from run_daily import date_hierarchy_dir, save_atomic

DEFAULT_STAGING_DIR = "/data/staging/tanshin"
DEFAULT_RAW_DIR = "/data/raw"
PROGRESS_FILENAME = "_progress.json"
EXIT_ABORTED = 3
DEFAULT_LOG_PATH = "/data/logs/backfill-tanshin.log"
DEFAULT_SEED_CSV_PATH = "/app/seed/edinet_common_stock_issuers.csv"
DEFAULT_REQUEST_DELAY = 1.0
DEFAULT_TIME_BUDGET_SECONDS = 5.5 * 3600
DEFAULT_SINCE = "2024-03-01"


class BackfillAborted(Exception):
    """レート制限の兆候・失敗率の上昇で処理を自動中断する。"""


class AbortGuard:
    """リクエストの結果を観測し、レート制限の兆候や失敗率の上昇で中断を判断する。

    - 連続 consecutive_limit 回、レート制限らしいエラー（429/403/503、PDF以外の応答）
    - 直近 slow_window 回の応答時間の平均が slow_seconds 超（サーバー側の絞り込み）
    - 試行 min_attempts 件以上で失敗率が max_failure_rate 超
    """

    RATE_LIMIT_MARKERS = ("status=429", "status=403", "status=503", "PDFとして期待される形式ではありません")

    def __init__(
        self,
        consecutive_limit: int = 3,
        slow_window: int = 10,
        slow_seconds: float = 8.0,
        min_attempts: int = 50,
        max_failure_rate: float = 0.05,
    ) -> None:
        self.consecutive_limit = consecutive_limit
        self.slow_seconds = slow_seconds
        self.min_attempts = min_attempts
        self.max_failure_rate = max_failure_rate
        self._consecutive = 0
        self._durations: deque[float] = deque(maxlen=slow_window)
        self.attempts = 0
        self.failures = 0

    def record_success(self, duration: float) -> None:
        self.attempts += 1
        self._consecutive = 0
        self._durations.append(duration)
        self._check_slow()

    def record_failure(self, error: str, duration: float) -> None:
        self.attempts += 1
        self.failures += 1
        self._durations.append(duration)
        if any(m in error for m in self.RATE_LIMIT_MARKERS):
            self._consecutive += 1
            if self._consecutive >= self.consecutive_limit:
                raise BackfillAborted(f"レート制限の兆候: 連続{self._consecutive}回のエラー（直近: {error}）")
        else:
            self._consecutive = 0
        self._check_slow()
        if self.attempts >= self.min_attempts and self.failures / self.attempts > self.max_failure_rate:
            raise BackfillAborted(f"失敗率が高い: {self.failures}/{self.attempts}件")

    def _check_slow(self) -> None:
        window = self._durations
        if window.maxlen is not None and len(window) == window.maxlen and sum(window) / len(window) > self.slow_seconds:
            raise BackfillAborted(f"応答が遅い状態が続く: 直近{len(window)}回の平均 {sum(window) / len(window):.1f}秒")


def load_progress(staging_dir: Path) -> dict[str, dict[str, int]]:
    path = staging_dir / PROGRESS_FILENAME
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    companies: dict[str, dict[str, int]] = data.get("companies", {})
    return companies


def save_progress(staging_dir: Path, companies: dict[str, dict[str, int]], since_iso: str) -> None:
    payload = json.dumps({"since": since_iso, "companies": companies}, ensure_ascii=False).encode("utf-8")
    save_atomic(staging_dir / PROGRESS_FILENAME, payload)


def load_tanshin_companies(csv_path: Path) -> list[dict[str, str]]:
    """seedの会社一覧。edinet_codeの重複行（seedに実在する）は先頭だけ残す。"""
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for c in load_reit_companies(csv_path):  # 列は共通（edinet_code, sec_code, filer_name）
        if c["edinet_code"] in seen:
            continue
        seen.add(c["edinet_code"])
        out.append(c)
    return out


def build_metadata(
    company: dict[str, str], match: jpx_disclosure_client.JpxDisclosure
) -> dict[str, str | None]:
    """run_daily.build_pdf_metadataと同じキー（tdnet_*を除く）。"""
    return {
        "edinet_code": company["edinet_code"],
        "sec_code": company["sec_code"],
        "company_name": company["filer_name"],
        "disclosure_kind": "kessan_tanshin",
        "jpx_disclosure_date": match.disclosure_date,
        "jpx_title": match.title,
        "pdf_url": match.pdf_url,
        "ixbrl_url": match.ixbrl_url,
        "source": "tanshin_backfill",
    }


def backfill_company(
    client: RetryingHttpClient,
    company: dict[str, str],
    data_dir: Path,
    raw_dir: Path,
    since_iso: str,
    with_ixbrl: bool,
    dry_run: bool,
    delay: float,
    stats: BackfillStats,
    logger: logging.Logger,
    guard: AbortGuard,
) -> bool:
    """会社1社分を処理する。失敗が無く全件揃ったらTrue（進捗ファイルで完了扱いにする）。"""
    sec_code = company["sec_code"]
    edinet_code = company["edinet_code"]
    name = company["filer_name"]

    t0 = time.monotonic()
    try:
        html = jpx_disclosure_client.fetch_company_page(client, sec_code)
    except Exception as e:
        guard.record_failure(str(e), time.monotonic() - t0)
        raise
    guard.record_success(time.monotonic() - t0)
    disclosures = jpx_disclosure_client.parse_kessan_disclosures(html)
    kessan = [d for d in disclosures if jpx_disclosure_client.title_matches_kind(d.title, "kessan_tanshin")]
    in_range = [d for d in kessan if disclosure_date_to_iso(d.disclosure_date) >= since_iso]
    stats.kessan_found += len(in_range)
    logger.info(
        f"{edinet_code} {name} (sec_code={sec_code}): 決算短信{len(kessan)}件検出"
        f"（{since_iso}以降 {len(in_range)}件）"
    )
    if dry_run:
        for d in in_range:
            logger.info(f"  [dry-run] {d.disclosure_date} {d.title} {d.pdf_url} ixbrl={'あり' if d.ixbrl_url else 'なし'}")
        return True

    company_failed = 0
    for match in in_range:
        t0 = time.monotonic()
        try:
            basename = match.pdf_url.rsplit("/", 1)[-1]
            event_date_iso = disclosure_date_to_iso(match.disclosure_date)
            dest = date_hierarchy_dir(data_dir, event_date_iso) / edinet_code / basename

            if dest.exists() or (date_hierarchy_dir(raw_dir, event_date_iso) / edinet_code / basename).exists():
                stats.kessan_skipped_existing += 1
                continue

            body = jpx_disclosure_client.download_pdf(client, match.pdf_url)
            save_atomic(dest, body)

            if with_ixbrl and match.ixbrl_url:
                ixbrl_basename = match.ixbrl_url.rsplit("/", 1)[-1]
                ixbrl_body = jpx_disclosure_client.download_ixbrl(client, match.ixbrl_url)
                save_atomic(dest.parent / ixbrl_basename, ixbrl_body)

            # メタデータJSONは最後に書く（DWHはJSONをglobして対象を決めるため、PDFが揃ってから見せる）
            metadata_json = json.dumps(build_metadata(company, match), ensure_ascii=False, indent=2).encode(
                "utf-8"
            )
            save_atomic(dest.with_suffix(".json"), metadata_json)

            stats.kessan_downloaded += 1
            guard.record_success(time.monotonic() - t0)
            logger.info(f"  取得: {match.disclosure_date} {match.title}")
        except BackfillAborted:
            raise
        except Exception as e:
            stats.kessan_failed += 1
            company_failed += 1
            logger.error(f"{edinet_code} {name}: {match.disclosure_date} {match.title!r} 取得失敗 ({e})")
            guard.record_failure(str(e), time.monotonic() - t0)
        finally:
            time.sleep(delay)
    return company_failed == 0


def run(
    companies: list[dict[str, str]],
    data_dir: Path,
    raw_dir: Path,
    since_iso: str,
    with_ixbrl: bool,
    dry_run: bool,
    delay: float,
    time_budget_seconds: float,
    logger: logging.Logger,
    guard: AbortGuard | None = None,
) -> BackfillStats:
    stats = BackfillStats()
    guard = guard or AbortGuard()
    progress = {} if dry_run else load_progress(data_dir)
    pending = [c for c in companies if c["edinet_code"] not in progress]
    logger.info(f"進捗: 完了済み{len(companies) - len(pending)}社を飛ばし、残り{len(pending)}社")
    client = RetryingHttpClient()
    run_started = time.monotonic()
    try:
        for i, company in enumerate(pending):
            elapsed = time.monotonic() - run_started
            if elapsed >= time_budget_seconds:
                logger.info(f"時間予算({time_budget_seconds:.0f}秒)に達したため残り{len(pending) - i}社は次回以降に持ち越し")
                break
            before = (stats.kessan_found, stats.kessan_downloaded, stats.kessan_skipped_existing, stats.kessan_failed)
            try:
                complete = backfill_company(
                    client, company, data_dir, raw_dir, since_iso, with_ixbrl, dry_run, delay, stats, logger, guard
                )
                stats.companies_processed += 1
                if complete and not dry_run:
                    progress[company["edinet_code"]] = {
                        "found": stats.kessan_found - before[0],
                        "downloaded": stats.kessan_downloaded - before[1],
                        "skipped": stats.kessan_skipped_existing - before[2],
                    }
                    save_progress(data_dir, progress, since_iso)
            except BackfillAborted:
                raise
            except Exception as e:
                stats.companies_failed[company["edinet_code"]] = str(e)
                logger.error(f"{company['edinet_code']} {company['filer_name']}: 会社詳細ページ取得失敗 ({e})")
            time.sleep(delay)
    finally:
        client.close()
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sec-code", type=str, help="単一銘柄のみを対象にする（検証用、5桁）")
    parser.add_argument("--since", type=str, default=DEFAULT_SINCE, help=f"この開示日以降のみ取得 YYYY-MM-DD（既定{DEFAULT_SINCE}）")
    parser.add_argument("--with-ixbrl", action="store_true", help="iXBRL(.htm)も取得する（既定は取得しない）")
    parser.add_argument("--dry-run", action="store_true", help="決算短信の検出だけ行い、ダウンロードしない")
    args = parser.parse_args()

    data_dir = Path(os.environ.get("STAGING_DIR", DEFAULT_STAGING_DIR))
    raw_dir = Path(os.environ.get("RAW_DIR", DEFAULT_RAW_DIR))
    log_path = os.environ.get("LOG_PATH", DEFAULT_LOG_PATH)
    seed_csv_path = Path(os.environ.get("SEED_CSV_PATH", DEFAULT_SEED_CSV_PATH))
    delay = float(os.environ.get("REQUEST_DELAY", DEFAULT_REQUEST_DELAY))
    time_budget_seconds = float(os.environ.get("TIME_BUDGET_SECONDS", DEFAULT_TIME_BUDGET_SECONDS))

    logger = setup_logger(log_path)
    data_dir.mkdir(parents=True, exist_ok=True)

    companies = load_tanshin_companies(seed_csv_path)
    if args.sec_code:
        companies = [c for c in companies if c["sec_code"] == args.sec_code]
        if not companies:
            logger.error(f"sec_code={args.sec_code}はseedに存在しません")
            sys.exit(1)

    logger.info(f"対象{len(companies)}社の決算短信バックフィルを開始（{args.since}以降、dry_run={args.dry_run}）")
    try:
        stats = run(
            companies, data_dir, raw_dir, args.since, args.with_ixbrl, args.dry_run, delay, time_budget_seconds, logger
        )
    except BackfillAborted as e:
        logger.error(f"自動中断: {e}。進捗は保存済み。間隔をあけて再実行してください")
        sys.exit(EXIT_ABORTED)
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

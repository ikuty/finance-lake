#!/usr/bin/env python3
"""TDnet（release.tdnet.info）の日次適時開示一覧を監視し、対象銘柄
（seed/edinet_common_stock_issuers.csv、普通株式発行体のみ）の決算短信・
業績予想の修正を検知する。検知した開示については、東証上場会社情報サービス
（www2.jpx.co.jp）から対応するPDFを直接取得する（TDnet自体のPDFは二次利用が
禁止されているため使わない。設計の詳細はdocs/jpx_disclosure_design.md参照）。

このリポジトリはファイルの取得・格納（レイク層）に特化する。書類の解釈（パース）
は後段の別リポジトリ（finance-dwh）の責務とし、ここでは扱わない。

Usage:
    python3 run_daily.py                    # 前日からTDNET_DAYS_WINDOW日分を対象に日次実行
    python3 run_daily.py --date 2026-08-07  # 単発日付を対象に実行（検証用）
    python3 run_daily.py --days 7 --force   # 直近7日分を対象に、取得済みの日付も再取得
    python3 run_daily.py --skip-pdf         # TDnet検知のみ行い、PDF取得は行わない

設定は環境変数から読む(Dockerの--env-fileを想定):
    DB_PATH              省略時 /data/ir_disclosure.db
    DATA_DIR             省略時 /data/raw
    LOG_PATH             省略時 /data/logs/ir-disclosure-dl.log
    SEED_CSV_PATH        省略時 /app/seed/edinet_common_stock_issuers.csv
    TDNET_DAYS_WINDOW    省略時 2。--days未指定時に日次実行で遡る日数
    REQUEST_DELAY        省略時 2.0 (秒)。JPX上場会社情報サービスへの銘柄ごとの
                         リクエスト間隔（ボット対策は実機確認上見られないが、
                         節度あるアクセスのため間隔を空ける）
    TIME_BUDGET_SECONDS  省略時 5400 (90分)。電源枠を使い切らないための安全弁
                         （edinet-dlと同じ設計、詳細はそちらのCLAUDE.md参照）
    SLACK_WEBHOOK_URL    省略可。設定時のみ実行結果をSlackへ通知する
    S3_BUCKET_NAME       省略可。設定時のみ実行状況レポートをS3へアップロードし、
                         公開URLをSlack通知に含める
"""
from __future__ import annotations

import argparse
import csv
import datetime
import json
import logging
import os
import shutil
import sqlite3
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path

import boto3

import db
import jpx_disclosure_client
import status_report
import tdnet_client
from http_client import RetryingHttpClient

DEFAULT_DB_PATH = "/data/ir_disclosure.db"
DEFAULT_DATA_DIR = "/data/raw"
DEFAULT_LOG_PATH = "/data/logs/ir-disclosure-dl.log"
DEFAULT_SEED_CSV_PATH = "/app/seed/edinet_common_stock_issuers.csv"
DEFAULT_TDNET_DAYS_WINDOW = 2
DEFAULT_REQUEST_DELAY = 2.0
DEFAULT_TIME_BUDGET_SECONDS = 90 * 60
DEFAULT_S3_REGION = "ap-northeast-1"
S3_REPORT_KEY = "ir-disclosure-dl/run_report.html"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5


@dataclass
class RunStats:
    tdnet_days_processed: list[str] = field(default_factory=list)
    tdnet_days_failed: dict[str, str] = field(default_factory=dict)
    new_events: int = 0
    pdf_downloaded: int = 0
    pdf_skipped: int = 0
    pdf_error: int = 0


def setup_logger(log_path: str) -> logging.Logger:
    logger = logging.getLogger("ir-disclosure-dl")
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


def load_seed_companies(conn: sqlite3.Connection, csv_path: Path) -> int:
    count = 0
    with csv_path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            sec_code = row["sec_code"].strip()
            jpx_code = sec_code[:4]
            db.upsert_company(conn, row["edinet_code"].strip(), sec_code, jpx_code, row["filer_name"].strip())
            count += 1
    conn.commit()
    return count


def date_hierarchy_dir(base_dir: Path, date_str: str) -> Path:
    """日付文字列(YYYY-MM-DD)をyyyy/mm/ddの3階層ディレクトリに分解する
    （edinet-dlと同じ設計、1年365個のディレクトリがbase_dir直下にフラットに
    並ぶのを避けるため）。"""
    year, month, day = date_str.split("-")
    return base_dir / year / month / day


def save_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / (path.name + ".tmp")
    tmp_path.write_bytes(data)
    os.replace(tmp_path, path)


def process_tdnet_watch(
    conn: sqlite3.Connection,
    client: tdnet_client.HttpClientLike,
    dates: list[datetime.date],
    force: bool,
    stats: RunStats,
    logger: logging.Logger,
) -> None:
    for d in dates:
        date_str = d.isoformat()
        if not force and db.already_done_tdnet(conn, date_str):
            continue
        try:
            n = tdnet_client.run_for_date(conn, client, date_str)
            stats.tdnet_days_processed.append(date_str)
            stats.new_events += n
            logger.info(f"{date_str}: TDnet検知 {n}件")
        except Exception as e:
            db.store_tdnet_progress(conn, date_str, "error", 0, str(e))
            stats.tdnet_days_failed[date_str] = str(e)
            logger.error(f"{date_str}: TDnet取得エラー ({e})")


def process_pdf_downloads(
    conn: sqlite3.Connection,
    client: jpx_disclosure_client.HttpClientLike,
    data_dir: Path,
    delay: float,
    stats: RunStats,
    logger: logging.Logger,
) -> None:
    pending = db.pending_tdnet_events(conn)
    logger.info(f"PDF取得対象: {len(pending)}件")

    for event in pending:
        row = conn.execute(
            "SELECT sec_code FROM companies WHERE edinet_code = ?", (event["edinet_code"],)
        ).fetchone()
        sec_code = row[0] if row else None
        if sec_code is None:
            logger.error(f"event_id={event['id']}: companiesにsec_codeが見つかりません")
            continue

        try:
            html = jpx_disclosure_client.fetch_company_page(client, sec_code)
            disclosures = jpx_disclosure_client.parse_kessan_disclosures(html)
            match = jpx_disclosure_client.select_matching_disclosure(
                disclosures, event["event_date"], event["disclosure_kind"]
            )
            if match is None:
                db.record_pdf_download(
                    conn, event["id"], None, None, "", "skipped",
                    None, "JPX上場会社情報サービス側に対応する開示が見つかりませんでした",
                )
                stats.pdf_skipped += 1
                continue

            body = jpx_disclosure_client.download_pdf(client, match.pdf_url)
            basename = match.pdf_url.rsplit("/", 1)[-1]
            dest = date_hierarchy_dir(data_dir, event["event_date"]) / event["edinet_code"] / basename
            save_atomic(dest, body)
            db.record_pdf_download(
                conn, event["id"], match.disclosure_date, match.title, match.pdf_url,
                "downloaded", str(dest), None,
            )
            stats.pdf_downloaded += 1
        except Exception as e:
            db.record_pdf_download(
                conn, event["id"], None, None, "", "error", None, str(e),
            )
            stats.pdf_error += 1
            logger.error(f"event_id={event['id']} {event['company_name']}: PDF取得失敗 ({e})")
        finally:
            time.sleep(delay)


def build_slack_message(stats: RunStats) -> str:
    lines = ["✅ ir-disclosure-dl 日次実行"]
    if stats.tdnet_days_failed:
        lines[0] = f"❌ ir-disclosure-dl 日次実行 一部失敗 ({len(stats.tdnet_days_failed)}日)"
        for date_str, message in stats.tdnet_days_failed.items():
            lines.append(f"TDnet失敗: {date_str} ({message})")
    lines.append(f"TDnet処理日数: {len(stats.tdnet_days_processed)}日 / 新規検知: {stats.new_events}件")
    lines.append(
        f"PDF取得: 成功{stats.pdf_downloaded}件 / 未一致{stats.pdf_skipped}件 / 失敗{stats.pdf_error}件"
    )
    return "\n".join(lines)


def send_slack_notification(webhook_url: str, message: str, logger: logging.Logger) -> None:
    try:
        payload = json.dumps({"text": message, "unfurl_links": False, "unfurl_media": False}).encode("utf-8")
        req = urllib.request.Request(
            webhook_url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception as e:
        logger.error(f"Slack通知の送信に失敗しました: {e}")


def upload_report_to_s3(db_path: Path, logger: logging.Logger) -> str | None:
    bucket = os.environ.get("S3_BUCKET_NAME")
    if not bucket:
        return None
    region = os.environ.get("AWS_DEFAULT_REGION", DEFAULT_S3_REGION)
    try:
        html, _summary = status_report.generate_report_html(db_path)
        s3 = boto3.client("s3")
        s3.put_object(
            Bucket=bucket, Key=S3_REPORT_KEY, Body=html.encode("utf-8"), ContentType="text/html; charset=utf-8",
        )
        return f"http://{bucket}.s3-website-{region}.amazonaws.com/{S3_REPORT_KEY}"
    except Exception as e:
        logger.error(f"実行状況レポートのS3アップロードに失敗しました: {e}")
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    default_days = int(os.environ.get("TDNET_DAYS_WINDOW", DEFAULT_TDNET_DAYS_WINDOW))
    parser.add_argument("--date", type=str, help="単発日付 YYYY-MM-DD（検証用、--daysより優先）")
    parser.add_argument("--days", type=int, default=default_days, help="前日から遡って何日分を対象にするか")
    parser.add_argument("--force", action="store_true", help="TDnet取得済みの日付も再取得する")
    parser.add_argument("--skip-pdf", action="store_true", help="TDnet検知のみ行い、PDF取得は行わない")
    args = parser.parse_args()

    db_path = Path(os.environ.get("DB_PATH", DEFAULT_DB_PATH))
    data_dir = Path(os.environ.get("DATA_DIR", DEFAULT_DATA_DIR))
    log_path = os.environ.get("LOG_PATH", DEFAULT_LOG_PATH)
    seed_csv_path = Path(os.environ.get("SEED_CSV_PATH", DEFAULT_SEED_CSV_PATH))
    delay = float(os.environ.get("REQUEST_DELAY", DEFAULT_REQUEST_DELAY))
    time_budget_seconds = float(os.environ.get("TIME_BUDGET_SECONDS", DEFAULT_TIME_BUDGET_SECONDS))
    slack_webhook_url = os.environ.get("SLACK_WEBHOOK_URL")

    logger = setup_logger(log_path)
    conn = db.init_db(db_path)
    data_dir.mkdir(parents=True, exist_ok=True)

    n_seed = load_seed_companies(conn, seed_csv_path)
    logger.info(f"seed取り込み: {n_seed}社")

    if args.date:
        dates = [datetime.date.fromisoformat(args.date)]
    else:
        last_complete_day = tdnet_client.last_complete_day_jst()
        dates = [last_complete_day - datetime.timedelta(days=i) for i in range(args.days)][::-1]

    stats = RunStats()
    client = RetryingHttpClient()
    run_started = time.monotonic()
    try:
        process_tdnet_watch(conn, client, dates, args.force, stats, logger)

        if not args.skip_pdf:
            elapsed = time.monotonic() - run_started
            if elapsed < time_budget_seconds:
                process_pdf_downloads(conn, client, data_dir, delay, stats, logger)
            else:
                logger.info("時間予算に達したためPDF取得は次回以降に持ち越し")
    finally:
        client.close()

    message = build_slack_message(stats)
    logger.info("summary: " + message.replace("\n", " / "))

    report_url = upload_report_to_s3(db_path, logger)
    if report_url:
        message = message + "\n\n📊 実行状況レポート: " + report_url
        logger.info(f"実行状況レポートをアップロードしました: {report_url}")

    if slack_webhook_url:
        send_slack_notification(slack_webhook_url, message, logger)

    try:
        free_bytes = shutil.disk_usage(data_dir).free
        logger.info(f"空き容量: {free_bytes / (1024**3):.1f}GB")
    except OSError:
        pass


if __name__ == "__main__":
    main()

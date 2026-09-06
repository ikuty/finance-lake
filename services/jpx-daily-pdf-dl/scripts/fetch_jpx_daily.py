#!/usr/bin/env python3
"""日本取引所グループ（JPX）の株式相場表（東証日報）を取得し、生データのまま保存する。
取得状況はSQLite(fetch_progress)へ記録する。設計の詳細は docs/file_download_design.md
を参照。

個人利用限定（JPX利用規約により商用目的の二次利用は不可）。

このスクリプトが継続的に担うのは以下の2形式のみ（詳細はdocs/file_download_design.md
「サービス本体と使い捨てスクリプトの切り分け」参照）。

  - 形式C（詳細日次、直近13ヶ月程度のローリングウィンドウ）:
    index.html・00-archives-01ページから日付->URLを解決して取得する。
  - 形式B（月次簡易OHLC）のうち03.htmlに現在列挙されている月:
    03.htmlは「当年進行中の月」ではなく、確定済みアーカイブへの移行がまだ済んで
    いない月を示すページ（実機確認、2026-09-05）。列挙されている中で最新の月は
    随時更新される可能性があるため常に取得し直し、それより前の月は一度成功して
    いればスキップする。

形式A（レガシー日次、1981-2019年）・形式Bの確定済み過去年分は、今後増えることも
変わることも無い確定データのため、このスクリプトの対象外（一回限りの使い捨て
バックフィルスクリプトで別途取得する）。

「今日」・「前日」はJSTで評価する（edinet-dlと同じ理由。ジョブは営業開始前に実行
されるため、「今日」を対象に含めても常に空振りになる）。

Usage:
    python3 fetch_jpx_daily.py                # DAYS_WINDOW日分（既定3日）を対象に実行
    python3 fetch_jpx_daily.py --days 7

設定は環境変数から読む(Dockerの --env-file を想定):
    DB_PATH              省略時 /data/index.db
    DATA_DIR             省略時 /data/raw
    LOG_PATH             省略時 /data/logs/jpx-daily-pdf-dl.log
    DAYS_WINDOW          省略時 3。--days未指定時に対象とする、前日から遡る日数
    SLACK_WEBHOOK_URL    省略可。設定時のみ実行結果をSlackへ通知する（edinet-dlと同じ設計）
    S3_BUCKET_NAME       省略可。設定時のみバックフィル進捗レポートをS3へアップロード
                         し、公開URLをSlack通知に含める（2026-09-06追加。詳細は
                         docs/file_download_design.md「バックフィル進捗レポートの
                         公開（S3）」参照）
    AWS_ACCESS_KEY_ID・AWS_SECRET_ACCESS_KEY・AWS_DEFAULT_REGION
                         boto3が自動で読む標準の環境変数名（本スクリプトは直接読まない）
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterator

import boto3

import backfill_report

JST = datetime.timezone(datetime.timedelta(hours=9))

DEFAULT_DB_PATH = "/data/index.db"
DEFAULT_DATA_DIR = "/data/raw"
DEFAULT_LOG_PATH = "/data/logs/jpx-daily-pdf-dl.log"
DEFAULT_DAYS_WINDOW = 3
# バックフィル進捗レポートのアップロード先（S3、任意）。バケットのリージョンは
# AWS_DEFAULT_REGION（boto3が自動で読む）と一致させる。静的サイトホスティングの
# 公開URL形式はリージョンによって異なりうるため、実際にデプロイしたバケットで
# 実機確認済みの形式を使う（2026-09-06）。
DEFAULT_S3_REGION = "ap-northeast-1"
S3_REPORT_KEY = "jpx-daily-pdf-dl/backfill_report.html"
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5MB
LOG_BACKUP_COUNT = 5

BASE_HOST = "www.jpx.co.jp"
DAILY_INDEX_PATH = "/markets/statistics-equities/daily/index.html"
DAILY_ARCHIVE_PATH = "/markets/statistics-equities/daily/00-archives-01.html"
MONTHLY_CURRENT_PATH = "/markets/statistics-equities/daily/03.html"
USER_AGENT = "Mozilla/5.0 (compatible; jpx-daily-pdf-dl/1.0)"

FORMAT_DETAILED_DAILY = "detailed-daily"
FORMAT_MONTHLY_OHLC = "monthly-ohlc"

# 一覧・アーカイブページ内の日次PDFへのリンク（例: .../stq_20260901.pdf）
STQ_LINK_RE = re.compile(r'href="([^"]*stq_(\d{8})\.pdf)"')
# 03.html内の月次PDFへのリンク（例: .../tvdivq0000001jan-att/202501.pdf）
MONTHLY_LINK_RE = re.compile(r'href="([^"]*/(\d{6})\.pdf)"')


class RateLimitedError(RuntimeError):
    pass


@dataclass
class RunStats:
    """1回の実行（形式C・形式Bの両方）のサマリ。Slack通知用に集計するだけの一時的な
    構造で、永続化はしない（edinet-dlと同じ設計）。"""

    processed: list[tuple[str, str]] = field(default_factory=list)  # (period, format)
    failed: dict[tuple[str, str], str] = field(default_factory=dict)
    downloaded_count: int = 0
    downloaded_bytes: int = 0
    retry_count: int = 0


def today_jst() -> datetime.date:
    return datetime.datetime.now(JST).date()


def last_complete_day_jst() -> datetime.date:
    """営業開始前に実行されるジョブが「今日」を対象に含めて空振りするのを防ぐため、
    対象期間の終端は常に前日とする（edinet-dlと同じ理由）。"""
    return today_jst() - datetime.timedelta(days=1)


def date_range(start: datetime.date, end: datetime.date) -> Iterator[datetime.date]:
    d = start
    while d <= end:
        yield d
        d += datetime.timedelta(days=1)


def setup_logger(log_path: str) -> logging.Logger:
    logger = logging.getLogger("jpx-daily-pdf-dl")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger  # 同一プロセス内で複数回呼ばれても二重登録しない（テスト等）

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(log_path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fetch_progress (
            period TEXT,       -- 'YYYY-MM-DD'（形式C） or 'YYYY-MM'（形式B）
            format TEXT,       -- 'detailed-daily' | 'monthly-ohlc'
            status TEXT,       -- 'done' | 'error'
            sourceUrl TEXT,
            message TEXT,
            fetchedAt TEXT,
            PRIMARY KEY (period, format)
        )
    """)
    conn.commit()
    return conn


def already_done(conn: sqlite3.Connection, period: str, fmt: str) -> bool:
    row = conn.execute(
        "SELECT status FROM fetch_progress WHERE period = ? AND format = ?", (period, fmt)
    ).fetchone()
    return row is not None and row[0] == "done"


def store_progress(
    conn: sqlite3.Connection, period: str, fmt: str, status: str, source_url: str | None, message: str | None
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO fetch_progress (period, format, status, sourceUrl, message, fetchedAt) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (period, fmt, status, source_url, message, datetime.datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()


def _http_get(url: str, max_retries: int = 5, stats: RunStats | None = None) -> bytes:
    """共通のHTTPフェッチ+リトライ。429・ネットワークエラー/タイムアウト・5xxはリトライ対象
    （最大5回、指数バックオフ）、それ以外の4xx（404等）は即座に呼び出し元へ伝播させる
    （呼び出し元が404を「まだ確定パスに存在しない」の意味で扱うことがあるため）。statsを
    渡すと、リトライが発生するたびに`retry_count`をインクリメントする（Slack通知向け、
    edinet-dlの429カウンタと同じ位置づけ。ただしここでは429・5xx・ネットワークエラーを
    区別せずまとめて数える）。"""
    attempt = 0
    while True:
        attempt += 1
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body: bytes = resp.read()
                return body
        except urllib.error.HTTPError as e:
            retryable = e.code == 429 or 500 <= e.code < 600
            if not retryable:
                raise
            if attempt > max_retries:
                raise RateLimitedError(f"{url}: リトライ上限に達しました (status={e.code})") from e
            if stats is not None:
                stats.retry_count += 1
            time.sleep(min(60, 2**attempt))
        except (urllib.error.URLError, OSError) as e:
            if attempt > max_retries:
                raise RuntimeError(f"{url}: ネットワークエラーが続くためリトライ上限に達しました ({e})") from e
            if stats is not None:
                stats.retry_count += 1
            time.sleep(min(60, 2**attempt))


def parse_daily_links(html: str) -> dict[str, str]:
    """一覧・アーカイブページのHTMLから、stq_YYYYMMDD.pdfへのリンクを抽出し、
    日付文字列(YYYY-MM-DD) -> 相対パスの対応表を返す。"""
    result: dict[str, str] = {}
    for path, yyyymmdd in STQ_LINK_RE.findall(html):
        date_str = f"{yyyymmdd[0:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"
        result[date_str] = path
    return result


def parse_monthly_links(html: str) -> dict[str, str]:
    """03.htmlのHTMLから、{yyyymm}.pdfへのリンクを抽出し、
    年月文字列(YYYY-MM) -> 相対パスの対応表を返す。"""
    result: dict[str, str] = {}
    for path, yyyymm in MONTHLY_LINK_RE.findall(html):
        ym_str = f"{yyyymm[0:4]}-{yyyymm[4:6]}"
        result[ym_str] = path
    return result


def date_hierarchy_dir(base_dir: Path, date_str: str) -> Path:
    """日付文字列(YYYY-MM-DD)をyyyy/mm/ddの3階層ディレクトリに分解する
    （edinet-dlと同じ理由。フラットなディレクトリの見通しの悪さを避けるため）。"""
    year, month, day = date_str.split("-")
    return base_dir / year / month / day


def detailed_daily_path(data_dir: Path, date_str: str) -> Path:
    return date_hierarchy_dir(data_dir / "detailed-daily", date_str) / "stq.pdf"


def monthly_ohlc_path(data_dir: Path, year_month: str) -> Path:
    year, month = year_month.split("-")
    return data_dir / "monthly-ohlc" / year / month / "stq_monthly.pdf"


def save_atomic(path: Path, data: bytes) -> None:
    """一時ファイル→renameでアトミックに保存する。中断時に壊れたファイルが残らないようにする。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / (path.name + ".tmp")
    tmp_path.write_bytes(data)
    os.replace(tmp_path, path)


def fetch_detailed_daily(
    conn: sqlite3.Connection,
    data_dir: Path,
    days_window: int,
    logger: logging.Logger,
    stats: RunStats | None = None,
    force: bool = False,
) -> None:
    """形式C（詳細日次）を、前日から days_window 日分さかのぼって取得する。
    index.html・00-archives-01ページを読んで日付->URLの対応表を作り、対象日が
    そこに含まれていればダウンロードする（含まれない＝週末・休日で提出が無いか、
    ローリングウィンドウの範囲外）。

    force=Trueの場合、DBの状態・ファイルの存在の両方を無視して必ず再ダウンロードする
    （2026-09-06決定。「forceは現在の状態を無視して取得する」という定義そのものであり、
    edinet-dlの--force（1日=複数ファイルという粒度で、個々のファイルは存在すれば
    スキップする）とは意図的に異なる。jpxは1期間=1ファイルのため、その粒度の使い分けが
    そもそも成立しない）。force=Falseの場合のみ、DBに記録が無いのにファイルだけ既に
    存在するケース（自己修復）で無駄なネットワークアクセスを避けるため`dest.exists()`
    を見る。statsを渡すとSlack通知向けの集計（processed/failed/ダウンロード件数・
    サイズ）を記録する。"""
    if stats is None:
        stats = RunStats()
    index_html = _http_get(f"https://{BASE_HOST}{DAILY_INDEX_PATH}", stats=stats).decode("utf-8", errors="ignore")
    archive_html = _http_get(f"https://{BASE_HOST}{DAILY_ARCHIVE_PATH}", stats=stats).decode(
        "utf-8", errors="ignore"
    )
    links = parse_daily_links(archive_html)
    links.update(parse_daily_links(index_html))  # 重複する日付はindex.html側を優先

    end = last_complete_day_jst()
    start = end - datetime.timedelta(days=days_window - 1)

    for d in date_range(start, end):
        date_str = d.isoformat()
        if not force and already_done(conn, date_str, FORMAT_DETAILED_DAILY):
            continue

        rel_path = links.get(date_str)
        if rel_path is None:
            continue  # 一覧に無い（週末・休日、またはローリングウィンドウの範囲外）

        url = f"https://{BASE_HOST}{rel_path}"
        dest = detailed_daily_path(data_dir, date_str)
        if not force and dest.exists():
            store_progress(conn, date_str, FORMAT_DETAILED_DAILY, "done", url, None)
            stats.processed.append((date_str, FORMAT_DETAILED_DAILY))
            continue

        try:
            body = _http_get(url, stats=stats)
            save_atomic(dest, body)
            store_progress(conn, date_str, FORMAT_DETAILED_DAILY, "done", url, None)
            stats.processed.append((date_str, FORMAT_DETAILED_DAILY))
            stats.downloaded_count += 1
            stats.downloaded_bytes += len(body)
            logger.info(f"{date_str} ({FORMAT_DETAILED_DAILY}): 取得成功")
        except Exception as e:
            store_progress(conn, date_str, FORMAT_DETAILED_DAILY, "error", url, str(e))
            stats.failed[(date_str, FORMAT_DETAILED_DAILY)] = str(e)
            logger.error(f"{date_str} ({FORMAT_DETAILED_DAILY}): 取得失敗 ({e})")


def fetch_monthly_recent(
    conn: sqlite3.Connection,
    data_dir: Path,
    logger: logging.Logger,
    stats: RunStats | None = None,
    force: bool = False,
) -> None:
    """形式B（月次簡易OHLC）のうち、03.htmlに現在列挙されている月を取得する。

    実機確認（2026-09-05）の結果、03.htmlは「当年進行中の月」ではなく、確定済み
    アーカイブへの移行がまだ済んでいない月を示すページだと分かった（実際、本稿執筆
    時点で2025年1〜8月のみが列挙されており、当年（2026年）分は一切現れない）。
    どの月が列挙されるかはJPX側の移行状況次第で変動するため、固定の「当月」を
    仮定せず、ページに実際に列挙されている月をそのまま対象にする。

    列挙されている中で最新の月は、確定前でまだ更新される可能性があるため常に
    取得し直す。それより前の月は、一度成功していれば変わらないためスキップする
    （force=Trueの場合はこのスキップも行わない）。statsを渡すとSlack通知向けの
    集計を記録する。
    """
    if stats is None:
        stats = RunStats()
    html = _http_get(f"https://{BASE_HOST}{MONTHLY_CURRENT_PATH}", stats=stats).decode("utf-8", errors="ignore")
    links = parse_monthly_links(html)
    if not links:
        logger.info(f"{MONTHLY_CURRENT_PATH}: 列挙されている月が無い")
        return

    latest_year_month = max(links)
    for year_month, rel_path in sorted(links.items()):
        if not force and year_month != latest_year_month and already_done(conn, year_month, FORMAT_MONTHLY_OHLC):
            continue

        url = f"https://{BASE_HOST}{rel_path}"
        try:
            body = _http_get(url, stats=stats)
            dest = monthly_ohlc_path(data_dir, year_month)
            save_atomic(dest, body)
            store_progress(conn, year_month, FORMAT_MONTHLY_OHLC, "done", url, None)
            stats.processed.append((year_month, FORMAT_MONTHLY_OHLC))
            stats.downloaded_count += 1
            stats.downloaded_bytes += len(body)
            logger.info(f"{year_month} ({FORMAT_MONTHLY_OHLC}): 取得成功")
        except Exception as e:
            store_progress(conn, year_month, FORMAT_MONTHLY_OHLC, "error", url, str(e))
            stats.failed[(year_month, FORMAT_MONTHLY_OHLC)] = str(e)
            logger.error(f"{year_month} ({FORMAT_MONTHLY_OHLC}): 取得失敗 ({e})")


def format_bytes(n: int) -> str:
    mb = n / (1024 * 1024)
    if mb >= 1024:
        return f"{mb / 1024:.1f}GB"
    return f"{mb:.1f}MB"


# Slack通知向けの表示名（形式A/B/Cという内部の分類名はドキュメント上の便宜的な呼び名で
# あり、通知を見る側には意味を持たないため使わない。2026-09-06修正）。
FORMAT_LABELS: dict[str, str] = {
    FORMAT_DETAILED_DAILY: "詳細日次",
    FORMAT_MONTHLY_OHLC: "月次OHLC（簡易）",
}


def _format_counts(stats: RunStats, fmt: str) -> str:
    label = FORMAT_LABELS[fmt]
    n_processed = sum(1 for p in stats.processed if p[1] == fmt)
    n_failed = sum(1 for p in stats.failed if p[1] == fmt)
    if n_failed:
        return f"{label}: 処理{n_processed + n_failed}件 / 成功{n_processed}件 / 失敗{n_failed}件"
    return f"{label}: 処理{n_processed}件 / 成功{n_processed}件"


def build_slack_message(stats: RunStats, free_bytes: int) -> str:
    free_gb = free_bytes / (1024**3)

    if stats.failed:
        lines = [f"❌ jpx-daily-pdf-dl 日次実行 失敗 ({len(stats.failed)}件)"]
    else:
        lines = ["✅ jpx-daily-pdf-dl 日次実行 成功"]

    lines.append(_format_counts(stats, FORMAT_DETAILED_DAILY))
    lines.append(_format_counts(stats, FORMAT_MONTHLY_OHLC))

    for (period, fmt), message in stats.failed.items():
        lines.append(f"失敗: {period} ({FORMAT_LABELS[fmt]}) ({message})")

    lines.append(f"ダウンロード: {stats.downloaded_count}件 / {format_bytes(stats.downloaded_bytes)}")
    lines.append(f"リトライ発生: {stats.retry_count}回")
    lines.append(f"空き容量: {free_gb:.1f}GB")
    return "\n".join(lines)


def send_slack_notification(webhook_url: str, message: str, logger: logging.Logger) -> None:
    """Slackへの通知失敗はログに記録するのみで、例外は上げない（ジョブ全体の成否に
    影響させない。edinet-dlと同じ設計）。unfurl_links/unfurl_mediaを無効化する
    （有効のままだとメッセージに含まれるURLの大きなプレビューカードが表示され、
    テキスト部分が視覚的に埋もれてしまうことをjpx-daily-pdf-dl-deploy.ymlの
    Slack通知で実機確認済み、2026-09-06）。"""
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
    """バックフィル進捗レポート（backfill_report.py）を生成し、S3へアップロードする。
    アップロードしたオブジェクトの公開URL（静的サイトホスティング経由）を返す。
    `S3_BUCKET_NAME`が未設定、またはアップロードに失敗した場合はNoneを返す。
    失敗はログに記録するのみで、例外は上げない（ジョブ全体の成否に影響させない、
    Slack通知と同じ設計。2026-09-06追加。GitHub Actionsのデプロイワークフロー経由
    だった旧方式を置き換えた——GitHub Actions以外の経路で結果が確認できないという
    設計上の問題があったため、日次実行のたびにここでアップロードする形に変更した）。"""
    bucket = os.environ.get("S3_BUCKET_NAME")
    if not bucket:
        return None

    region = os.environ.get("AWS_DEFAULT_REGION", DEFAULT_S3_REGION)
    try:
        html, _summary = backfill_report.generate_report_html(db_path)
        s3 = boto3.client("s3")
        s3.put_object(
            Bucket=bucket, Key=S3_REPORT_KEY,
            Body=html.encode("utf-8"), ContentType="text/html; charset=utf-8",
        )
        return f"http://{bucket}.s3-website-{region}.amazonaws.com/{S3_REPORT_KEY}"
    except Exception as e:
        logger.error(f"バックフィルレポートのS3アップロードに失敗しました: {e}")
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    default_days = int(os.environ.get("DAYS_WINDOW", DEFAULT_DAYS_WINDOW))
    parser.add_argument(
        "--days", type=int, default=default_days,
        help=f"前日から遡って形式C（詳細日次）を何日分対象にするか（既定{default_days}）",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="既にdoneな期間も対象に含める（既存ファイルは引き続き存在チェックでスキップされる）",
    )
    args = parser.parse_args()

    db_path = Path(os.environ.get("DB_PATH", DEFAULT_DB_PATH))
    data_dir = Path(os.environ.get("DATA_DIR", DEFAULT_DATA_DIR))
    log_path = os.environ.get("LOG_PATH", DEFAULT_LOG_PATH)
    slack_webhook_url = os.environ.get("SLACK_WEBHOOK_URL")

    logger = setup_logger(log_path)
    conn = init_db(db_path)
    data_dir.mkdir(parents=True, exist_ok=True)

    stats = RunStats()
    fetch_detailed_daily(conn, data_dir, args.days, logger, stats, force=args.force)
    fetch_monthly_recent(conn, data_dir, logger, stats, force=args.force)

    try:
        free_bytes = shutil.disk_usage(data_dir).free
    except OSError:
        free_bytes = 0

    message = build_slack_message(stats, free_bytes)
    logger.info("summary: " + message.replace("\n", " / "))

    report_url = upload_report_to_s3(db_path, logger)
    if report_url:
        message = message + "\n\n📊 バックフィル進捗レポート: " + report_url
        logger.info(f"バックフィルレポートをアップロードしました: {report_url}")

    if slack_webhook_url:
        send_slack_notification(slack_webhook_url, message, logger)


if __name__ == "__main__":
    main()

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
    DB_PATH      省略時 /data/index.db
    DATA_DIR     省略時 /data/raw
    LOG_PATH     省略時 /data/logs/jpx-daily-pdf-dl.log
    DAYS_WINDOW  省略時 3。--days未指定時に対象とする、前日から遡る日数
"""
from __future__ import annotations

import argparse
import datetime
import logging
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterator

JST = datetime.timezone(datetime.timedelta(hours=9))

DEFAULT_DB_PATH = "/data/index.db"
DEFAULT_DATA_DIR = "/data/raw"
DEFAULT_LOG_PATH = "/data/logs/jpx-daily-pdf-dl.log"
DEFAULT_DAYS_WINDOW = 3
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


def _http_get(url: str, max_retries: int = 5) -> bytes:
    """共通のHTTPフェッチ+リトライ。429・ネットワークエラー/タイムアウト・5xxはリトライ対象
    （最大5回、指数バックオフ）、それ以外の4xx（404等）は即座に呼び出し元へ伝播させる
    （呼び出し元が404を「まだ確定パスに存在しない」の意味で扱うことがあるため）。"""
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
            time.sleep(min(60, 2**attempt))
        except (urllib.error.URLError, OSError) as e:
            if attempt > max_retries:
                raise RuntimeError(f"{url}: ネットワークエラーが続くためリトライ上限に達しました ({e})") from e
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
    conn: sqlite3.Connection, data_dir: Path, days_window: int, logger: logging.Logger
) -> None:
    """形式C（詳細日次）を、前日から days_window 日分さかのぼって取得する。
    index.html・00-archives-01ページを読んで日付->URLの対応表を作り、対象日が
    そこに含まれていればダウンロードする（含まれない＝週末・休日で提出が無いか、
    ローリングウィンドウの範囲外）。"""
    index_html = _http_get(f"https://{BASE_HOST}{DAILY_INDEX_PATH}").decode("utf-8", errors="ignore")
    archive_html = _http_get(f"https://{BASE_HOST}{DAILY_ARCHIVE_PATH}").decode("utf-8", errors="ignore")
    links = parse_daily_links(archive_html)
    links.update(parse_daily_links(index_html))  # 重複する日付はindex.html側を優先

    end = last_complete_day_jst()
    start = end - datetime.timedelta(days=days_window - 1)

    for d in date_range(start, end):
        date_str = d.isoformat()
        if already_done(conn, date_str, FORMAT_DETAILED_DAILY):
            continue

        rel_path = links.get(date_str)
        if rel_path is None:
            continue  # 一覧に無い（週末・休日、またはローリングウィンドウの範囲外）

        url = f"https://{BASE_HOST}{rel_path}"
        dest = detailed_daily_path(data_dir, date_str)
        if dest.exists():
            store_progress(conn, date_str, FORMAT_DETAILED_DAILY, "done", url, None)
            continue

        try:
            body = _http_get(url)
            save_atomic(dest, body)
            store_progress(conn, date_str, FORMAT_DETAILED_DAILY, "done", url, None)
            logger.info(f"{date_str} ({FORMAT_DETAILED_DAILY}): 取得成功")
        except Exception as e:
            store_progress(conn, date_str, FORMAT_DETAILED_DAILY, "error", url, str(e))
            logger.error(f"{date_str} ({FORMAT_DETAILED_DAILY}): 取得失敗 ({e})")


def fetch_monthly_recent(conn: sqlite3.Connection, data_dir: Path, logger: logging.Logger) -> None:
    """形式B（月次簡易OHLC）のうち、03.htmlに現在列挙されている月を取得する。

    実機確認（2026-09-05）の結果、03.htmlは「当年進行中の月」ではなく、確定済み
    アーカイブへの移行がまだ済んでいない月を示すページだと分かった（実際、本稿執筆
    時点で2025年1〜8月のみが列挙されており、当年（2026年）分は一切現れない）。
    どの月が列挙されるかはJPX側の移行状況次第で変動するため、固定の「当月」を
    仮定せず、ページに実際に列挙されている月をそのまま対象にする。

    列挙されている中で最新の月は、確定前でまだ更新される可能性があるため常に
    取得し直す。それより前の月は、一度成功していれば変わらないためスキップする。
    """
    html = _http_get(f"https://{BASE_HOST}{MONTHLY_CURRENT_PATH}").decode("utf-8", errors="ignore")
    links = parse_monthly_links(html)
    if not links:
        logger.info(f"{MONTHLY_CURRENT_PATH}: 列挙されている月が無い")
        return

    latest_year_month = max(links)
    for year_month, rel_path in sorted(links.items()):
        if year_month != latest_year_month and already_done(conn, year_month, FORMAT_MONTHLY_OHLC):
            continue

        url = f"https://{BASE_HOST}{rel_path}"
        try:
            body = _http_get(url)
            dest = monthly_ohlc_path(data_dir, year_month)
            save_atomic(dest, body)
            store_progress(conn, year_month, FORMAT_MONTHLY_OHLC, "done", url, None)
            logger.info(f"{year_month} ({FORMAT_MONTHLY_OHLC}): 取得成功")
        except Exception as e:
            store_progress(conn, year_month, FORMAT_MONTHLY_OHLC, "error", url, str(e))
            logger.error(f"{year_month} ({FORMAT_MONTHLY_OHLC}): 取得失敗 ({e})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    default_days = int(os.environ.get("DAYS_WINDOW", DEFAULT_DAYS_WINDOW))
    parser.add_argument(
        "--days", type=int, default=default_days,
        help=f"前日から遡って形式C（詳細日次）を何日分対象にするか（既定{default_days}）",
    )
    args = parser.parse_args()

    db_path = Path(os.environ.get("DB_PATH", DEFAULT_DB_PATH))
    data_dir = Path(os.environ.get("DATA_DIR", DEFAULT_DATA_DIR))
    log_path = os.environ.get("LOG_PATH", DEFAULT_LOG_PATH)

    logger = setup_logger(log_path)
    conn = init_db(db_path)
    data_dir.mkdir(parents=True, exist_ok=True)

    fetch_detailed_daily(conn, data_dir, args.days, logger)
    fetch_monthly_recent(conn, data_dir, logger)


if __name__ == "__main__":
    main()

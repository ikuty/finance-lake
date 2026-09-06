#!/usr/bin/env python3
"""形式A（レガシー日次、1981年1月〜2019年12月）・形式B確定済み過去年分（2020年1月〜）を
一回限り取得する、使い捨てのバックフィルスクリプト。

この2つは今後増えることも変わることも無い確定データのため、サービス本体
（fetch_jpx_daily.py）から完全に切り離している（詳細はdocs/file_download_design.md
「サービス本体と使い捨てスクリプトの切り分け」参照）。systemdタイマーには含めず、
手動で実行する。

両形式ともパス直接指定（一覧ページのスクレイピング不要）で取得できる。

  - 形式A: https://www.jpx.co.jp/markets/statistics-equities/daily/data/{yyyymm}.zip
    中身は1日1PDFがそのまま束ねられたZIP（例: BO_C0076_20191202.pdf）。展開して
    個々の日次PDFとして保存する。
  - 形式B確定済み: https://www.jpx.co.jp/markets/statistics-equities/daily/data/{yyyymm}.pdf
    JPXが確定アーカイブへ移行済みの月のみ200を返す。移行はかなりの遅延（実機確認で
    約1年以上）があるため、直近の一定期間は404になる。404は「まだ確定していない」
    という想定内の状態としてスキップするだけで、エラー扱いにはしない
    （fetch_jpx_daily.pyのサービス本体側が引き続き03.html経由で取得を担当する）。

進捗はサービス本体と共有するfetch_progressテーブルに、月単位（'YYYY-MM'）で記録する
（2026-09-06決定。形式Aの内部粒度は日次ZIP展開だが、原子的な失敗単位はZIP=月であり、
backfill_report.pyが年×月のマス目で表示する都合とも合わせるため月単位に統一した）。

Usage:
    python3 backfill_confirmed_archive.py                    # 形式A・確定済み形式Bの両方
    python3 backfill_confirmed_archive.py --legacy-only       # 形式Aのみ
    python3 backfill_confirmed_archive.py --confirmed-only    # 確定済み形式Bのみ
    python3 backfill_confirmed_archive.py --force             # 既にdoneな月も再取得する

設定は環境変数から読む（fetch_jpx_daily.pyと共通のDB・データディレクトリを使う）:
    DB_PATH      省略時 /data/index.db
    DATA_DIR     省略時 /data/raw
"""
from __future__ import annotations

import argparse
import io
import logging
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_jpx_daily import (  # noqa: E402
    BASE_HOST,
    FORMAT_MONTHLY_OHLC,
    USER_AGENT,
    already_done,
    date_hierarchy_dir,
    init_db,
    monthly_ohlc_path,
    save_atomic,
    store_progress,
    today_jst,
)

# fetch_jpx_daily.pyには無い、本スクリプト（形式A）固有のformat値。
# backfill_report.pyが参照する値と一致させる必要がある。
FORMAT_LEGACY_DAILY = "legacy-daily"

LEGACY_START_YEAR_MONTH = (1981, 1)
LEGACY_END_YEAR_MONTH = (2019, 12)  # 両端含む
CONFIRMED_START_YEAR_MONTH = (2020, 1)

DEFAULT_DB_PATH = "/data/index.db"
DEFAULT_DATA_DIR = "/data/raw"

# ZIP内の日次PDFファイル名末尾から日付を抽出する（例: BO_C0076_20191202.pdf）。
# プレフィックス自体は年代によって変わりうるため、末尾の8桁日付のみに依存する。
DAILY_PDF_DATE_RE = re.compile(r"(\d{4})(\d{2})(\d{2})\.pdf$")


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("backfill-confirmed-archive")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


def year_month_range(start: tuple[int, int], end: tuple[int, int]) -> list[tuple[int, int]]:
    """startからendまで（両端含む）の(year, month)一覧を返す。"""
    months: list[tuple[int, int]] = []
    year, month = start
    while (year, month) <= end:
        months.append((year, month))
        month += 1
        if month > 12:
            month = 1
            year += 1
    return months


def _http_get(url: str, max_retries: int = 5) -> bytes:
    """fetch_jpx_daily._http_getと同じリトライ方針（429・5xx・ネットワークエラーは
    最大5回指数バックオフ、404等それ以外の4xxは即座に呼び出し元へ伝播）。"""
    attempt = 0
    while True:
        attempt += 1
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body: bytes = resp.read()
                return body
        except urllib.error.HTTPError as e:
            retryable = e.code == 429 or 500 <= e.code < 600
            if not retryable:
                raise
            if attempt > max_retries:
                raise RuntimeError(f"{url}: リトライ上限に達しました (status={e.code})") from e
            time.sleep(min(60, 2**attempt))
        except (urllib.error.URLError, OSError) as e:
            if attempt > max_retries:
                raise RuntimeError(f"{url}: ネットワークエラーが続くためリトライ上限に達しました ({e})") from e
            time.sleep(min(60, 2**attempt))


def legacy_daily_path(data_dir: Path, date_str: str) -> Path:
    return date_hierarchy_dir(data_dir / "legacy-daily", date_str) / "stq.pdf"


def extract_legacy_zip(zip_bytes: bytes, data_dir: Path, logger: logging.Logger) -> int:
    """ZIP内の日次PDFを個々に展開・保存する。戻り値は保存したファイル数。
    ファイル名から日付を抽出できないエントリは警告してスキップする。"""
    saved_count = 0
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            m = DAILY_PDF_DATE_RE.search(info.filename)
            if not m:
                logger.warning(f"日付を抽出できないためスキップ: {info.filename}")
                continue
            date_str = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
            dest = legacy_daily_path(data_dir, date_str)
            if dest.exists():
                continue
            save_atomic(dest, zf.read(info.filename))
            saved_count += 1
    return saved_count


def backfill_legacy(
    conn: sqlite3.Connection, data_dir: Path, logger: logging.Logger, force: bool = False
) -> None:
    """形式A（レガシー日次、1981年1月〜2019年12月）を月単位で取得する。"""
    months = year_month_range(LEGACY_START_YEAR_MONTH, LEGACY_END_YEAR_MONTH)
    for year, month in months:
        year_month = f"{year:04d}-{month:02d}"
        if not force and already_done(conn, year_month, FORMAT_LEGACY_DAILY):
            continue

        url = f"https://{BASE_HOST}/markets/statistics-equities/daily/data/{year:04d}{month:02d}.zip"
        try:
            zip_bytes = _http_get(url)
            saved_count = extract_legacy_zip(zip_bytes, data_dir, logger)
            store_progress(conn, year_month, FORMAT_LEGACY_DAILY, "done", url, None)
            logger.info(f"{year_month} (legacy-daily): 取得成功（{saved_count}日分）")
        except Exception as e:
            store_progress(conn, year_month, FORMAT_LEGACY_DAILY, "error", url, str(e))
            logger.error(f"{year_month} (legacy-daily): 取得失敗 ({e})")


def backfill_confirmed(
    conn: sqlite3.Connection, data_dir: Path, logger: logging.Logger, force: bool = False
) -> None:
    """形式B確定済み過去年分（2020年1月〜前月）を月単位で取得する。JPX側の確定
    アーカイブへの移行がまだの月は404になるため、これはエラー扱いにせずスキップする
    （移行済みかどうかを実際に確認するのが本スクリプトの役割の一つ）。"""
    end_year, end_month = today_jst().year, today_jst().month
    if end_month == 1:
        end = (end_year - 1, 12)
    else:
        end = (end_year, end_month - 1)

    for year, month in year_month_range(CONFIRMED_START_YEAR_MONTH, end):
        year_month = f"{year:04d}-{month:02d}"
        if not force and already_done(conn, year_month, FORMAT_MONTHLY_OHLC):
            continue

        url = f"https://{BASE_HOST}/markets/statistics-equities/daily/data/{year:04d}{month:02d}.pdf"
        try:
            body = _http_get(url)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                logger.info(f"{year_month} (monthly-ohlc): 確定アーカイブへ未移行のためスキップ")
                continue
            store_progress(conn, year_month, FORMAT_MONTHLY_OHLC, "error", url, str(e))
            logger.error(f"{year_month} (monthly-ohlc): 取得失敗 ({e})")
            continue
        except Exception as e:
            store_progress(conn, year_month, FORMAT_MONTHLY_OHLC, "error", url, str(e))
            logger.error(f"{year_month} (monthly-ohlc): 取得失敗 ({e})")
            continue

        dest = monthly_ohlc_path(data_dir, year_month)
        save_atomic(dest, body)
        store_progress(conn, year_month, FORMAT_MONTHLY_OHLC, "done", url, None)
        logger.info(f"{year_month} (monthly-ohlc): 取得成功")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--legacy-only", action="store_true", help="形式A（1981-2019）のみ対象にする")
    parser.add_argument("--confirmed-only", action="store_true", help="確定済み形式B（2020-前月）のみ対象にする")
    parser.add_argument(
        "--force", action="store_true",
        help="既にdoneな月も対象に含める（既存ファイルは引き続き存在チェックでスキップされる）",
    )
    args = parser.parse_args()

    db_path = Path(os.environ.get("DB_PATH", DEFAULT_DB_PATH))
    data_dir = Path(os.environ.get("DATA_DIR", DEFAULT_DATA_DIR))

    logger = setup_logger()
    conn = init_db(db_path)
    data_dir.mkdir(parents=True, exist_ok=True)

    run_legacy = not args.confirmed_only
    run_confirmed = not args.legacy_only

    if run_legacy:
        backfill_legacy(conn, data_dir, logger, force=args.force)
    if run_confirmed:
        backfill_confirmed(conn, data_dir, logger, force=args.force)


if __name__ == "__main__":
    main()

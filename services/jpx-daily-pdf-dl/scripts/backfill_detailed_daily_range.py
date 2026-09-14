#!/usr/bin/env python3
"""形式C（詳細日次）のうち、ローリングウィンドウ内ではあるがサービス本体
（fetch_jpx_daily.py）がまだ取得していない過去の期間を、一回限り取得する
使い捨てのバックフィルスクリプト。

fetch_jpx_daily.py本体は、日々の運用に必要な直近数日分の取得にしか使わない
`index.html`・`00-archives-01.html`（＝直近1ヶ月分）しか読まない。しかし実際の
ローリングウィンドウは`00-archives-01.html`〜`00-archives-12.html`（実機確認、
2026-09-14: 01=2026年8月、02=2026年7月、...、12=2025年9月。13以降は空）の
12ページ＋index.html（当月）で計13ヶ月分をカバーしている。サービスの稼働開始が
ウィンドウの途中だった等の理由で、ウィンドウ内なのに未取得の過去月が生じうる
（実際に2026-09-14時点で、detailed-dailyは2026年08-09月分しか無く、2026年
01-07月分が未取得のまま残っていた）。本スクリプトはそのギャップを埋める。

サービス本体と同じfetch_progress（'YYYY-MM-DD' / 'detailed-daily'）・
同じdetailed-daily/{yyyy}/{mm}/{dd}/stq.pdfパスを共有するため、実行後は
サービス本体の次回実行時にも二重取得されない。

Usage:
    python3 backfill_detailed_daily_range.py --start 2026-01-01 --end 2026-06-30
    python3 backfill_detailed_daily_range.py --start 2026-01-01 --end 2026-06-30 --force

設定は環境変数から読む（fetch_jpx_daily.pyと共通のDB・データディレクトリを使う）:
    DB_PATH      省略時 /data/index.db
    DATA_DIR     省略時 /data/raw
"""
from __future__ import annotations

import argparse
import datetime
import logging
import os
import sqlite3
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_jpx_daily import (  # noqa: E402
    BASE_HOST,
    FORMAT_DETAILED_DAILY,
    RunStats,
    _http_get,
    already_done,
    date_range,
    detailed_daily_path,
    init_db,
    parse_daily_links,
    save_atomic,
    store_progress,
)

DEFAULT_DB_PATH = "/data/index.db"
DEFAULT_DATA_DIR = "/data/raw"

# ローリングウィンドウの上限ページ番号。実機確認(2026-09-14)では13ページ目以降が
# 空だったため12だが、将来ウィンドウ幅が変わっても取りこぼさないよう少し余裕を持つ。
MAX_ARCHIVE_PAGES = 15


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("backfill-detailed-daily-range")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


def fetch_archive_links(logger: logging.Logger, max_pages: int = MAX_ARCHIVE_PAGES) -> dict[str, str]:
    """00-archives-01.html〜NNページを順に取得し、日付(YYYY-MM-DD)→相対パスの
    対応表を返す。リンクが1件も無いページ、または404に達した時点で、それより先は
    無いと判断して打ち切る（実機確認、2026-09-14: curlでは13ページ目以降が
    200かつリンク無しだったが、実行時は13ページ目が404を返すこともあった。
    ページの存在確認自体がJPX側で揺れうるため、両方を「窓の終端」として扱う）。"""
    links: dict[str, str] = {}
    for page in range(1, max_pages + 1):
        url = f"https://{BASE_HOST}/markets/statistics-equities/daily/00-archives-{page:02d}.html"
        try:
            html = _http_get(url).decode("utf-8", errors="ignore")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                logger.info(f"00-archives-{page:02d}.html: 404、ここで打ち切り")
                break
            raise
        page_links = parse_daily_links(html)
        if not page_links:
            logger.info(f"00-archives-{page:02d}.html: リンク無し、ここで打ち切り")
            break
        logger.info(f"00-archives-{page:02d}.html: {len(page_links)}件")
        links.update(page_links)
    return links


def backfill_range(
    conn: sqlite3.Connection,
    data_dir: Path,
    start: datetime.date,
    end: datetime.date,
    logger: logging.Logger,
    links: dict[str, str],
    force: bool = False,
) -> RunStats:
    """[start, end]（両端含む）の各日について、linksに含まれていれば取得する。
    linksに無い日は週末・休日、またはローリングウィンドウの範囲外として無視する。
    """
    stats = RunStats()
    for d in date_range(start, end):
        date_str = d.isoformat()
        if not force and already_done(conn, date_str, FORMAT_DETAILED_DAILY):
            continue

        rel_path = links.get(date_str)
        if rel_path is None:
            continue

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
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", required=True, help="対象期間の開始日 (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="対象期間の終了日 (YYYY-MM-DD, 両端含む)")
    parser.add_argument(
        "--force", action="store_true",
        help="既にdoneな日も対象に含める（既存ファイルは引き続き存在チェックでスキップされる）",
    )
    args = parser.parse_args()

    start = datetime.date.fromisoformat(args.start)
    end = datetime.date.fromisoformat(args.end)

    db_path = Path(os.environ.get("DB_PATH", DEFAULT_DB_PATH))
    data_dir = Path(os.environ.get("DATA_DIR", DEFAULT_DATA_DIR))

    logger = setup_logger()
    conn = init_db(db_path)
    data_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"対象期間: {start} 〜 {end}")
    links = fetch_archive_links(logger)
    stats = backfill_range(conn, data_dir, start, end, logger, links, force=args.force)

    n_ok = len(stats.processed)
    n_ng = len(stats.failed)
    logger.info(f"完了: 成功{n_ok}件 / 失敗{n_ng}件 / ダウンロード{stats.downloaded_count}件")
    if stats.failed:
        for (period, _fmt), message in stats.failed.items():
            logger.error(f"失敗: {period} ({message})")
        sys.exit(1)


if __name__ == "__main__":
    main()

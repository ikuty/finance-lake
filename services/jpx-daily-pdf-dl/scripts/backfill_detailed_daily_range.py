#!/usr/bin/env python3
"""形式C（詳細日次）のうち、ローリングウィンドウ内ではあるがサービス本体
（fetch_jpx_daily.py）がまだ取得していない過去の期間を、一回限り取得する
使い捨てのバックフィルスクリプト。

fetch_jpx_daily.py本体は、日々の運用に必要な直近数日分の取得にしか使わない
（対象期間にまたがる年月ぶんだけ`fetch_month_links`を呼ぶ）。しかし実際の
ローリングウィンドウは13ヶ月程度をカバーしている。サービスの稼働開始が
ウィンドウの途中だった等の理由で、ウィンドウ内なのに未取得の過去月が生じうる
（実際に2026-09-14時点で、detailed-dailyは2026年08-09月分しか無く、2026年
01-07月分が未取得のまま残っていた）。本スクリプトはそのギャップを埋める。

対象期間にまたがる年月を`months_in_range`で列挙し、月ごとに`fetch_month_links`
（JPXが2026-09-18前後に実施したページ構造変更に対応済み、詳細はfetch_jpx_daily.py
のモジュールdocstring参照）で日付→URLを解決する。

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
    fetch_month_links,
    init_db,
    months_in_range,
    save_atomic,
    store_progress,
)

DEFAULT_DB_PATH = "/data/index.db"
DEFAULT_DATA_DIR = "/data/raw"


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("backfill-detailed-daily-range")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


def fetch_archive_links(
    start: datetime.date, end: datetime.date, logger: logging.Logger
) -> dict[str, str]:
    """[start, end]にまたがる年月ごとにfetch_month_linksを呼び、日付(YYYY-MM-DD)→
    相対パスの対応表を返す（該当月のデータが無ければ空のまま、fetch_month_links
    自身が404を吸収する）。"""
    links: dict[str, str] = {}
    for ym in months_in_range(start, end):
        month_links = fetch_month_links(ym)
        logger.info(f"{ym}: {len(month_links)}件")
        links.update(month_links)
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
    links = fetch_archive_links(start, end, logger)
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

#!/usr/bin/env python3
"""EDINET書類一覧API (https://api.edinet-fsa.go.jp/api/v2/documents.json) を
日付単位で呼び出し、日付ごとの取得状況をSQLite(fetch_progress)に記録する。

このリポジトリはファイルの取得・格納（レイク層）に特化する。書類メタデータの
列展開・検索用インデックス作成、及び中身の解釈（パース）は後段の別リポジトリの
責務とし、ここでは扱わない。

実行モードは2通り:
  - 初回バックフィル(手動): --start-date/--end-date で過去10年分程度を指定
  - 日次更新(cron): --daysを省略（環境変数DAYS_WINDOW、既定3日分の遡り窓で実行）

中断・再開可能な設計(fetch_progressテーブルで日付ごとの取得済み状態を記録)。--daysの
窓を1日より広く取ることで、電源障害等でジョブが実行されなかった日や、一時的な失敗で
errorになった日も、後続の実行で自動的に再試行される。

Usage:
    python3 build_index.py                    # DAYS_WINDOW日分（既定3日）を対象に日次実行
    python3 build_index.py --start-date 2016-08-13 --end-date 2026-08-13
    python3 build_index.py --days 7 --force   # 取得済みの日付も再取得

設定は環境変数から読む(Dockerの --env-file を想定):
    EDINET_API_KEY  必須。EDINET APIキー
    DB_PATH         省略時 /data/edinet_index.db
    REQUEST_DELAY   省略時 1.2 (秒)
    DAYS_WINDOW     省略時 3。--days未指定時に日次実行で遡る日数
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterator

DEFAULT_DB_PATH = "/data/edinet_index.db"
DEFAULT_DELAY = 1.2
DEFAULT_DAYS_WINDOW = 3

API_BASE = "https://api.edinet-fsa.go.jp/api/v2/documents.json"
USER_AGENT = "Mozilla/5.0 (compatible; edinet-dl/1.0)"


class RateLimitedError(RuntimeError):
    pass


def load_api_key() -> str:
    key = os.environ.get("EDINET_API_KEY")
    if not key:
        raise RuntimeError("環境変数 EDINET_API_KEY が設定されていません")
    return key


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fetch_progress (
            fileDate TEXT PRIMARY KEY,
            status TEXT,       -- 'done' | 'error'
            docCount INTEGER,
            message TEXT,
            fetchedAt TEXT
        )
    """)
    conn.commit()
    return conn


def already_done(conn: sqlite3.Connection, date_str: str) -> bool:
    row = conn.execute(
        "SELECT status FROM fetch_progress WHERE fileDate = ?", (date_str,)
    ).fetchone()
    return row is not None and row[0] == "done"


def fetch_day(date_str: str, api_key: str, max_retries: int = 5) -> dict[str, Any]:
    url = f"{API_BASE}?date={date_str}&type=2&Subscription-Key={api_key}"
    attempt = 0
    while True:
        attempt += 1
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                http_status = resp.status
                body = resp.read()
        except urllib.error.HTTPError as e:
            http_status = e.code
            body = e.read()

        if http_status == 429:
            if attempt > max_retries:
                raise RateLimitedError(f"{date_str}: 429が続くためリトライ上限に達しました")
            time.sleep(min(60, 2 ** attempt))
            continue

        data: dict[str, Any] = json.loads(body.decode("utf-8"))
        status = str(data.get("metadata", {}).get("status", http_status))

        if status == "429":
            if attempt > max_retries:
                raise RateLimitedError(f"{date_str}: 429が続くためリトライ上限に達しました")
            time.sleep(min(60, 2 ** attempt))
            continue

        if status not in ("200", "OK"):
            message = data.get("metadata", {}).get("message", "unknown error")
            raise RuntimeError(f"{date_str}: EDINET APIエラー status={status} message={message}")

        return data


def store_day(conn: sqlite3.Connection, date_str: str, data: dict[str, Any]) -> int:
    results = data.get("results", [])
    conn.execute(
        "INSERT OR REPLACE INTO fetch_progress (fileDate, status, docCount, message, fetchedAt) "
        "VALUES (?, 'done', ?, NULL, ?)",
        (date_str, len(results), datetime.datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    return len(results)


def date_range(start: datetime.date, end: datetime.date) -> Iterator[datetime.date]:
    d = start
    while d <= end:
        yield d
        d += datetime.timedelta(days=1)


def run(
    conn: sqlite3.Connection,
    api_key: str,
    start: datetime.date,
    end: datetime.date,
    delay: float,
    force: bool,
) -> int:
    dates = list(date_range(start, end))
    todo = [d for d in dates if force or not already_done(conn, d.isoformat())]
    print(f"対象期間: {start} 〜 {end}（{len(dates)}日間）/ 未取得: {len(todo)}日", file=sys.stderr)

    total_docs = 0
    for i, d in enumerate(todo):
        date_str = d.isoformat()
        try:
            data = fetch_day(date_str, api_key)
            count = store_day(conn, date_str, data)
            total_docs += count
            print(f"[{i + 1}/{len(todo)}] {date_str}: {count}件", file=sys.stderr)
        except RateLimitedError as e:
            print(f"[{i + 1}/{len(todo)}] {date_str}: 中断 ({e})", file=sys.stderr)
            break
        except Exception as e:
            conn.execute(
                "INSERT OR REPLACE INTO fetch_progress (fileDate, status, docCount, message, fetchedAt) "
                "VALUES (?, 'error', 0, ?, ?)",
                (date_str, str(e), datetime.datetime.now().isoformat(timespec="seconds")),
            )
            conn.commit()
            print(f"[{i + 1}/{len(todo)}] {date_str}: エラー ({e})", file=sys.stderr)

        if i < len(todo) - 1:
            time.sleep(delay)

    print(f"完了。新規取得 {total_docs}件。", file=sys.stderr)
    return total_docs


def main() -> None:
    default_days = int(os.environ.get("DAYS_WINDOW", DEFAULT_DAYS_WINDOW))
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--days", type=int, default=default_days,
        help=f"今日から遡って何日分を対象にするか（省略時は環境変数DAYS_WINDOW、既定{default_days}）",
    )
    parser.add_argument("--start-date", type=str, help="開始日 YYYY-MM-DD（指定時は--daysより優先）")
    parser.add_argument("--end-date", type=str, help="終了日 YYYY-MM-DD（省略時は今日）")
    parser.add_argument("--force", action="store_true", help="取得済みの日付も再取得する")
    args = parser.parse_args()

    today = datetime.date.today()
    if args.start_date:
        start = datetime.date.fromisoformat(args.start_date)
        end = datetime.date.fromisoformat(args.end_date) if args.end_date else today
    elif args.days:
        end = today
        start = today - datetime.timedelta(days=args.days - 1)
    else:
        parser.error("--days か --start-date のいずれかを指定してください")

    api_key = load_api_key()
    db_path = Path(os.environ.get("DB_PATH", DEFAULT_DB_PATH))
    delay = float(os.environ.get("REQUEST_DELAY", DEFAULT_DELAY))
    conn = init_db(db_path)

    run(conn, api_key, start, end, delay, args.force)


if __name__ == "__main__":
    main()

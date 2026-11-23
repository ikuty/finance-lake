#!/usr/bin/env python3
"""J-Quants API (GET /v2/fins/summary) を対象期間の平日ごとに1回呼び出し、
生レスポンスをそのままローカルに保存する（レイク層、解釈・加工はしない）。

対象期間の既定値はFreeプランの取得可能範囲（直近12週を除く過去2年分）。
Lightプラン契約後に再実行する場合は--start-date/--end-dateで明示的に
指定すること（Freeプランの範囲に自動的に限定されてしまうため）。

休日判定はしていない（土日のみローカルで除外し、祝日はAPI呼び出し自体は行う。
件数0件のレスポンスが返るだけで実害が無く、取引カレンダーAPIを別途導入する
手間に対して得られる削減効果が小さいため見送った）。

リクエスト間隔は既定13秒（Freeプランのレート制限5回/分に対し約4.6回/分で
安全マージンを確保）。Lightプラン（60回/分）移行後は--delayで短縮できる。

中断・再開可能な設計: 保存先に該当日のファイルが既に存在する日はスキップする
（SQLite等の進捗テーブルは持たない。1日1ファイルの存在チェックのみで足りる
単純さのため、edinet-dlのfetch_progressテーブルほどの仕組みは不要と判断）。

設定は環境変数から読む(Dockerの --env-file を想定):
    JQUANTS_API_KEY  必須。J-Quantsダッシュボードで発行したAPIキー
    DATA_DIR         省略時 /data。保存先ルート

保存先パス: {DATA_DIR}/fins_summary/{date}.json（生レスポンスそのまま）

Usage:
    python3 fetch_all.py                              # Freeプランの既定範囲
    python3 fetch_all.py --start-date 2024-01-01 --end-date 2024-12-31
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import time
from pathlib import Path

from fetch_summary import fetch_summary, load_api_key

DEFAULT_DATA_DIR = "/data"
DEFAULT_DELAY_SECONDS = 13.0
FREE_PLAN_LAG_WEEKS = 12
FREE_PLAN_HISTORY_YEARS = 2


def default_date_range(today: datetime.date) -> tuple[datetime.date, datetime.date]:
    """Freeプランで取得可能な範囲（直近12週を除く過去2年分）を返す。"""
    end = today - datetime.timedelta(weeks=FREE_PLAN_LAG_WEEKS)
    start = end - datetime.timedelta(days=365 * FREE_PLAN_HISTORY_YEARS)
    return start, end


def weekdays(start: datetime.date, end: datetime.date) -> list[datetime.date]:
    """start〜end(両端含む)の土日を除いた日付一覧。祝日は除外しない。"""
    days = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            days.append(current)
        current += datetime.timedelta(days=1)
    return days


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start-date", type=str, help="開始日 YYYY-MM-DD（省略時はFreeプラン取得可能範囲の開始日）")
    parser.add_argument("--end-date", type=str, help="終了日 YYYY-MM-DD（省略時はFreeプラン取得可能範囲の終了日）")
    parser.add_argument(
        "--delay", type=float, default=DEFAULT_DELAY_SECONDS,
        help=f"リクエスト間隔(秒)、既定{DEFAULT_DELAY_SECONDS}",
    )
    args = parser.parse_args()

    default_start, default_end = default_date_range(datetime.date.today())
    start = datetime.date.fromisoformat(args.start_date) if args.start_date else default_start
    end = datetime.date.fromisoformat(args.end_date) if args.end_date else default_end

    api_key = load_api_key()
    out_dir = Path(os.environ.get("DATA_DIR", DEFAULT_DATA_DIR)) / "fins_summary"
    out_dir.mkdir(parents=True, exist_ok=True)

    days = weekdays(start, end)
    print(f"対象期間: {start} 〜 {end}（平日{len(days)}日分、保存先: {out_dir}）")

    fetched = 0
    skipped = 0
    for day in days:
        out_path = out_dir / f"{day.isoformat()}.json"
        if out_path.exists():
            skipped += 1
            continue
        result = fetch_summary(api_key, day.isoformat())
        out_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        fetched += 1
        data = result.get("data", [])
        count = len(data) if isinstance(data, list) else 0
        print(f"{day.isoformat()}: {count}件 保存")
        time.sleep(args.delay)

    print(f"完了: 新規取得{fetched}日、スキップ(既存){skipped}日")


if __name__ == "__main__":
    main()

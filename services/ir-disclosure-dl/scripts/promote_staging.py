#!/usr/bin/env python3
"""backfill_tanshin.py がステージングに取得した決算短信を、指定した月だけ raw/ へ昇格する。

raw/ に日付フォルダが出ると、DWHの夜間transformがバックログ取り込みを始めるため、
取得が全社分そろった月を、取り込む直前にまとめて昇格する（finance-dwh Issue #94）。

昇格の単位は <日付>/<edinet_code>/ ディレクトリ（同一ファイルシステム上のrenameで、
各社分は原子的）。raw側に同じ <edinet_code> ディレクトリが既にある場合は上書きせず
スキップする（冪等。再実行しても安全）。昇格後、空になったステージングの日付フォルダは削除する。

既定では、_progress.json の完了社数が seed の全社数に満たないと昇格しない
（取得が途中の月を昇格すると、残りが二度と拾われない問題が起きるため）。
--allow-incomplete で無効化できる。

Usage:
    python3 promote_staging.py --month 2026-05 --dry-run
    python3 promote_staging.py --month 2026-05 --month 2026-04

環境変数: STAGING_DIR(/data/staging/tanshin) RAW_DIR(/data/raw) SEED_CSV_PATH(/app/seed/edinet_common_stock_issuers.csv)
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from backfill_tanshin import DEFAULT_RAW_DIR, DEFAULT_STAGING_DIR, load_progress, load_tanshin_companies

DEFAULT_SEED_CSV_PATH = "/app/seed/edinet_common_stock_issuers.csv"
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


@dataclass
class PromoteStats:
    promoted: int = 0
    skipped_existing: int = 0
    dates: int = 0


def promote_month(staging_dir: Path, raw_dir: Path, month: str, dry_run: bool) -> PromoteStats:
    year, mm = month.split("-")
    stats = PromoteStats()
    month_dir = staging_dir / year / mm
    if not month_dir.is_dir():
        return stats
    for day_dir in sorted(p for p in month_dir.iterdir() if p.is_dir()):
        stats.dates += 1
        for company_dir in sorted(p for p in day_dir.iterdir() if p.is_dir()):
            dest = raw_dir / year / mm / day_dir.name / company_dir.name
            if dest.exists():
                stats.skipped_existing += 1
                continue
            stats.promoted += 1
            if dry_run:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            company_dir.rename(dest)
        if not dry_run and not any(day_dir.iterdir()):
            day_dir.rmdir()
    if not dry_run and month_dir.is_dir() and not any(month_dir.iterdir()):
        month_dir.rmdir()
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--month", action="append", required=True, help="昇格する月 YYYY-MM（複数指定可）")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-incomplete", action="store_true", help="取得が全社分そろっていなくても昇格する")
    args = parser.parse_args()

    for m in args.month:
        if not _MONTH_RE.match(m):
            parser.error(f"--month は YYYY-MM 形式: {m}")

    staging_dir = Path(os.environ.get("STAGING_DIR", DEFAULT_STAGING_DIR))
    raw_dir = Path(os.environ.get("RAW_DIR", DEFAULT_RAW_DIR))
    seed = Path(os.environ.get("SEED_CSV_PATH", DEFAULT_SEED_CSV_PATH))

    if not args.allow_incomplete:
        total = len(load_tanshin_companies(seed))
        done = len(load_progress(staging_dir))
        if done < total:
            print(f"取得が未完了です（完了{done}/{total}社）。--allow-incomplete で強制できます", file=sys.stderr)
            sys.exit(2)

    for month in sorted(args.month):
        s = promote_month(staging_dir, raw_dir, month, args.dry_run)
        label = "[dry-run] " if args.dry_run else ""
        print(f"{label}{month}: 日付{s.dates}日 昇格{s.promoted}社分 既存スキップ{s.skipped_existing}")


if __name__ == "__main__":
    main()

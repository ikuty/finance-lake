#!/usr/bin/env python3
"""既存の data/raw/{fileDate}/{edinetCode}/{docID}_{xbrl,csv}.zip を、
現在のfetch_documents.pyの保存方式（展開・個別gzip圧縮）に移行するための
一度きりの移行スクリプト。

2026-08-28、zip形式のまま保存する方式からの変更に伴い作成。設計の詳細は
docs/file_download_design.md「保存先パス」を参照。

各zipファイルについて:
  1. fetch_documents.extract_and_gzip() で展開・個別gzip圧縮（{docID}_xbrl/ 等へ）
  2. 展開に成功したら、元のzipファイルを削除
  3. 失敗した場合は元のzipファイルを残し、そのファイルパスをエラーとして報告する
     （他のファイルの処理は継続する）

冪等性: 展開先ディレクトリが既に存在する場合はスキップする（extract_and_gzip自体は
   一時ディレクトリ→renameのアトミック書き込みのため、中断しても不完全な状態は残らない）。
   元のzipファイルが既に削除済み（前回実行で移行済み）の場合は単に見つからず対象外になる。

Usage:
    python3 migrate_zip_to_gz.py [DATA_DIR]
    # DATA_DIR省略時は環境変数DATA_DIR、それも無ければ /data/raw
    python3 migrate_zip_to_gz.py --dry-run [DATA_DIR]  # 削除・変換を行わず対象一覧のみ表示
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_documents  # noqa: E402


def find_target_zips(data_dir: Path) -> list[Path]:
    return sorted(data_dir.rglob("*_xbrl.zip")) + sorted(data_dir.rglob("*_csv.zip"))


def migrate_one(zip_path: Path, dry_run: bool) -> tuple[bool, str]:
    """1つのzipファイルを移行する。戻り値は(成功したか, メッセージ)。"""
    dest_dir = zip_path.parent / zip_path.stem  # "S100AAAA_xbrl.zip" -> "S100AAAA_xbrl"

    if dest_dir.exists():
        return True, f"skip（展開先が既に存在）: {dest_dir}"

    if dry_run:
        return True, f"[dry-run] 変換対象: {zip_path}"

    zip_bytes = zip_path.read_bytes()
    try:
        file_count, total_bytes = fetch_documents.extract_and_gzip(zip_bytes, dest_dir)
    except Exception as e:
        return False, f"失敗（元のzipは削除しない）: {zip_path} ({e})"

    zip_path.unlink()
    return True, f"完了: {zip_path.name} -> {dest_dir.name}/ ({file_count}ファイル, {total_bytes}バイト)"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("data_dir", nargs="?", help="対象のDATA_DIR（省略時は環境変数DATA_DIRまたは/data/raw）")
    parser.add_argument("--dry-run", action="store_true", help="変換・削除を行わず対象一覧のみ表示する")
    args = parser.parse_args()

    data_dir = Path(args.data_dir or os.environ.get("DATA_DIR", "/data/raw"))
    if not data_dir.is_dir():
        print(f"DATA_DIRが存在しません: {data_dir}", file=sys.stderr)
        raise SystemExit(1)

    targets = find_target_zips(data_dir)
    print(f"対象zipファイル数: {len(targets)}", file=sys.stderr)

    success_count = 0
    failed: list[str] = []
    for i, zip_path in enumerate(targets):
        ok, message = migrate_one(zip_path, args.dry_run)
        print(f"[{i + 1}/{len(targets)}] {message}", file=sys.stderr)
        if ok:
            success_count += 1
        else:
            failed.append(str(zip_path))

    print(f"完了。成功 {success_count}/{len(targets)}件。", file=sys.stderr)
    if failed:
        print(f"失敗（元のzipを残しています）: {failed}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()

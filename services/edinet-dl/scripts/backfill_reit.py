#!/usr/bin/env python3
"""REIT（投資法人）の書類本体（XBRL/PDF/CSV）を、既に取得済みの書類一覧APIレスポンス
（data/response/{yyyy}/{mm}/{dd}/document_list.json）から遡及的にダウンロードする。

fetch_documents.pyのis_reit_form_code()追加（2026-10-11）は今後の日次実行にのみ適用
され、過去分は再ダウンロードされない。書類一覧API自体を再度呼び出す必要はない
（document_list.jsonはsecCode等で絞り込む前の生レスポンスをそのまま保存しており、
REITの書類も既に含まれているため）。保存済みの一覧ファイルをスキャンし、REITの
書類本体だけを対象に取得する。詳細はservices/ir-disclosure-dl/docs/
reit_support_design.md参照。

このリポジトリはファイルの取得・格納（レイク層）に特化する。書類の解釈（パース）は
後段の別リポジトリの責務とし、ここでは扱わない。

Usage:
    python3 backfill_reit.py                       # response/配下の全日付を対象に実行
    python3 backfill_reit.py --edinet-code E24347   # 単一銘柄のみ（検証用）

設定は環境変数から読む(fetch_documents.pyと同じ、Dockerの--env-fileを想定):
    DATA_DIR             省略時 /data/raw（fetch_documents.pyと同じ）
    EDINET_API_KEY       必須
    LOG_PATH             省略時 /data/logs/backfill-reit.log
    REQUEST_DELAY        省略時 0.5 (秒)
"""
from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
from pathlib import Path
from typing import Any

import fetch_documents

DEFAULT_DATA_DIR = "/data/raw"
DEFAULT_LOG_PATH = "/data/logs/backfill-reit.log"
DEFAULT_REQUEST_DELAY = 0.5
ENABLED_TYPES = {1, 2, 5}


def find_reit_docs(data_dir: Path, edinet_code_filter: str | None) -> list[tuple[str, dict[str, Any]]]:
    """response/配下の全document_list.jsonをスキャンし、REITの書類だけを
    (file_date, doc) のリストで返す。"""
    results: list[tuple[str, dict[str, Any]]] = []
    pattern = str(data_dir / "response" / "*" / "*" / "*" / "document_list.json")
    for path_str in sorted(glob.glob(pattern)):
        path = Path(path_str)
        day, month, year = path.parent.name, path.parent.parent.name, path.parent.parent.parent.name
        file_date = f"{year}-{month}-{day}"
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        for doc in data.get("results", []):
            if not fetch_documents.is_reit_form_code(doc.get("formCode")):
                continue
            if edinet_code_filter and doc.get("edinetCode") != edinet_code_filter:
                continue
            results.append((file_date, doc))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--edinet-code", type=str, help="単一銘柄のみを対象にする（検証用）")
    args = parser.parse_args()

    data_dir = Path(os.environ.get("DATA_DIR", DEFAULT_DATA_DIR))
    log_path = os.environ.get("LOG_PATH", DEFAULT_LOG_PATH)
    delay = float(os.environ.get("REQUEST_DELAY", DEFAULT_REQUEST_DELAY))
    api_key = fetch_documents.load_api_key()

    logger = fetch_documents.setup_logger(log_path)
    fetch_documents.force_ipv4()

    docs = find_reit_docs(data_dir, args.edinet_code)
    logger.info(f"対象{len(docs)}件のREIT書類本体バックフィルを開始")

    today = datetime.date.today()
    stats = fetch_documents.RunStats(start=today, end=today)
    client = fetch_documents.EdinetHttpClient()
    failed = 0
    try:
        for i, (file_date, doc) in enumerate(docs, start=1):
            ok = fetch_documents.download_doc_files(
                client, doc, file_date, data_dir, api_key, delay, stats, logger, ENABLED_TYPES
            )
            if not ok:
                failed += 1
            if i % 50 == 0:
                logger.info(f"進捗 {i}/{len(docs)}件処理済み")
    finally:
        client.close()

    logger.info(
        f"完了: {len(docs)}件処理 (失敗{failed}件) / "
        f"ダウンロード{stats.downloaded_count}ファイル ({stats.downloaded_bytes}バイト)"
    )


if __name__ == "__main__":
    main()

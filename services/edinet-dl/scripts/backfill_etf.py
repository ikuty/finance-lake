#!/usr/bin/env python3
"""確認済みETF(上場投資信託)の書類本体（XBRL/PDF/CSV）を、既に取得済みの書類一覧API
レスポンス（data/response/{yyyy}/{mm}/{dd}/document_list.json）から遡及的にダウンロード
する。backfill_reit.pyのETF版（詳細はfetch_documents.pyの冒頭「ETF対応について」参照）。

fetch_documents.pyのis_etf_fund()追加（2026-10-17）は今後の日次実行にのみ適用され、
過去分は再ダウンロードされない。書類一覧API自体を再度呼び出す必要はない
（document_list.jsonはsecCode等で絞り込む前の生レスポンスをそのまま保存しており、
ETFの書類も既に含まれているため）。保存済みの一覧ファイルをスキャンし、
seed/edinet_etf_fund_issuers.csvに含まれる(edinetCode, fundCode)の書類本体だけを
対象に取得する。

このリポジトリはファイルの取得・格納（レイク層）に特化する。書類の解釈（パース）は
後段の別リポジトリの責務とし、ここでは扱わない。

Usage:
    python3 backfill_etf.py                       # response/配下の全日付を対象に実行
    python3 backfill_etf.py --edinet-code E09096   # 単一運用会社のみ（検証用）

設定は環境変数から読む(fetch_documents.pyと同じ、Dockerの--env-fileを想定):
    DATA_DIR             省略時 /data/raw（fetch_documents.pyと同じ）
    EDINET_API_KEY       必須
    LOG_PATH             省略時 /data/logs/backfill-etf.log
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
DEFAULT_LOG_PATH = "/data/logs/backfill-etf.log"
DEFAULT_REQUEST_DELAY = 0.5
ENABLED_TYPES = {1, 2, 5}


def find_etf_docs(
    data_dir: Path, etf_fund_keys: set[tuple[str, str]], edinet_code_filter: str | None
) -> list[tuple[str, dict[str, Any]]]:
    """response/配下の全document_list.jsonをスキャンし、確認済みETFの書類だけを
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
            if not fetch_documents.is_etf_fund(doc.get("edinetCode"), doc.get("fundCode"), etf_fund_keys):
                continue
            if edinet_code_filter and doc.get("edinetCode") != edinet_code_filter:
                continue
            results.append((file_date, doc))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--edinet-code", type=str, help="単一運用会社のみを対象にする（検証用）")
    args = parser.parse_args()

    data_dir = Path(os.environ.get("DATA_DIR", DEFAULT_DATA_DIR))
    log_path = os.environ.get("LOG_PATH", DEFAULT_LOG_PATH)
    delay = float(os.environ.get("REQUEST_DELAY", DEFAULT_REQUEST_DELAY))
    api_key = fetch_documents.load_api_key()

    logger = fetch_documents.setup_logger(log_path)
    fetch_documents.force_ipv4()

    etf_fund_keys = fetch_documents.load_etf_fund_keys()
    logger.info(f"確認済みETF: {len(etf_fund_keys)}件")

    docs = find_etf_docs(data_dir, etf_fund_keys, args.edinet_code)
    logger.info(f"対象{len(docs)}件のETF書類本体バックフィルを開始")

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

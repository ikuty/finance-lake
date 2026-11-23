#!/usr/bin/env python3
"""J-Quants API (https://api.jquants.com) の財務情報エンドポイント
(GET /v2/fins/summary) を1日分だけ呼び出し、認証・取得が実際に動くことを
確認するための最小限の動作確認スクリプト。

当DWH（finance-dwh）が独自ロジックで算出した財務指標を、第三者データ
(J-Quants)で検証するための準備作業の第一歩。全期間のループ取得・ローカル
保存・DWHとの突き合わせは別スクリプトで扱う（このスクリプトはまだ含まない）。

認証はV2 APIの静的APIキー方式（ダッシュボードの[API Keys]画面で発行）。
ヘッダー x-api-key にそのまま値を渡す（Bearer等のプレフィックスは無し）。

dateのみを指定すると、その日に開示があった全銘柄分が1回のレスポンスで
返る（codeを指定しない限り銘柄を絞り込まない）。

設定は環境変数から読む(Dockerの --env-file を想定):
    JQUANTS_API_KEY  必須。J-Quantsダッシュボードで発行したAPIキー

Usage:
    python3 fetch_summary.py --date 2024-10-25
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://api.jquants.com/v2/fins/summary"


def load_api_key() -> str:
    api_key = os.environ.get("JQUANTS_API_KEY")
    if not api_key:
        print("エラー: 環境変数 JQUANTS_API_KEY が設定されていません。", file=sys.stderr)
        sys.exit(1)
    return api_key


def fetch_summary(api_key: str, date: str) -> dict[str, object]:
    url = f"{BASE_URL}?{urllib.parse.urlencode({'date': date})}"
    request = urllib.request.Request(url, headers={"x-api-key": api_key})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        print(f"エラー: HTTP {e.code} {e.reason}\n{detail}", file=sys.stderr)
        sys.exit(1)
    parsed: dict[str, object] = json.loads(body)
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", required=True, help="開示日 YYYY-MM-DD（この日に開示があった全銘柄分を取得）")
    args = parser.parse_args()

    api_key = load_api_key()
    result = fetch_summary(api_key, args.date)

    data = result.get("data", [])
    assert isinstance(data, list)
    print(f"件数: {len(data)}")
    if data:
        print("先頭1件:")
        print(json.dumps(data[0], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

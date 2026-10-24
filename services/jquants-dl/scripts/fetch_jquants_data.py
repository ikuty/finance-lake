#!/usr/bin/env python3
"""J-Quants API (https://api.jquants.com) から複数のエンドポイントを日次で呼び出し、
生レスポンスをそのまま保存する。

対象エンドポイント（ENDPOINTS）:
    daily_quotes   GET /v2/equities/bars/daily   株価四本値・調整後値（旧daily_quotes）
    listed_master  GET /v2/equities/master        銘柄マスタ（会社名・市場区分・業種等）

いずれも`date`のみを指定すると、その日の全上場銘柄分（実機確認、2026-10-20:
daily_quotes 4,443件・listed_master 4,443件、ETF/REIT/インフラファンド等含む）が
1レスポンスで返る（実機確認、pagination_keyは出現しなかったが、将来の仕様変更・
件数増加に備えてページネーションには対応する）。銘柄の絞り込みはしない（フィルタ・
解釈は後段finance-dwh側の責務、レイク層は生データをそのまま保存するという
`edinet-dl`等と同じ方針）。

東証日報PDF（`jpx-daily-pdf-dl`）とは独立に収集する（2026-10-24決定）。日報は
前場/後場別の明細を持つがjQuantsには無く、jQuantsは株式分割等の調整後値
（AdjO/AdjH/AdjL/AdjC/AdjVo）を持つが日報には無い。両者は完全な重複ではなく、
同じ取引日のデータが両ソースに存在する期間は相互検証にも使える。取捨選択は
mart層以降の責務とし、cleansed層までは独立したテーブルのまま持つ。

listed_masterは次営業日分（17:30以降）も取得可能という仕様だが、本サービスは
daily_quotesと同じ「直近DAYS_WINDOW日」の過去窓に揃える（対称性・単純さのため、
2026-10-25決定）。

J-Quantsが提供する他の有用なエンドポイント（財務情報・配当・売買内訳等）も、
同じ「1日1回・date指定で全銘柄分」という形であれば、ENDPOINTSに追加するだけで
対応できる設計にしている（1サービスに集約する方針、2026-10-24決定。既存の
`jpx-daily-pdf-dl`が形式A/B/Cを1サービスで扱うのと同じ考え方）。

取得対象は「直近DAYS_WINDOW日（既定3日、`edinet-dl`と同じ既定値）」のうち、
まだファイルが存在しない日×エンドポイントのみ（`edinet-dl`と同じローリング
ウィンドウ+冪等スキップ方式。実行し忘れた日があっても後続の実行で自動的に
追いつける）。1日1エンドポイント1ファイルの単純な構成のため、進捗管理用のDBは
持たない（ファイルの存在チェックのみで十分、`jquants-validate/fetch_all.py`と
同じ判断）。

設定は環境変数から読む(Dockerの --env-file を想定):
    JQUANTS_API_KEY  必須。J-Quantsダッシュボードで発行したAPIキー（V2、静的キー方式）
    DATA_DIR         省略時 /data/raw
    LOG_PATH         省略時 /data/logs/jquants-dl.log
    DAYS_WINDOW      省略時 3
    SLACK_WEBHOOK_URL 省略可。設定時のみ実行結果をSlackへ通知する

Usage:
    python3 fetch_jquants_data.py                  # 直近DAYS_WINDOW日×全エンドポイント(未取得のみ)
    python3 fetch_jquants_data.py --date 2026-10-20 # 指定日のみ取得（手動補完用）
    python3 fetch_jquants_data.py --force           # 既に取得済みの日も再取得する
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

JST = datetime.timezone(datetime.timedelta(hours=9))

ENDPOINTS: dict[str, str] = {
    "daily_quotes": "https://api.jquants.com/v2/equities/bars/daily",
    "listed_master": "https://api.jquants.com/v2/equities/master",
}

DEFAULT_DATA_DIR = "/data/raw"
DEFAULT_LOG_PATH = "/data/logs/jquants-dl.log"
DEFAULT_DAYS_WINDOW = 3
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5


def today_jst() -> datetime.date:
    return datetime.datetime.now(JST).date()


def setup_logger(log_path: str) -> logging.Logger:
    logger = logging.getLogger("jquants-dl")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger  # 同一プロセス内で複数回呼ばれても二重登録しない（テスト等）

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(log_path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger


def load_api_key() -> str:
    api_key = os.environ.get("JQUANTS_API_KEY")
    if not api_key:
        print("エラー: 環境変数 JQUANTS_API_KEY が設定されていません。", file=sys.stderr)
        sys.exit(1)
    return api_key


def _fetch_page(
    base_url: str, api_key: str, date_str: str, pagination_key: str | None, max_retries: int = 5
) -> dict[str, Any]:
    """1ページ分を取得する。DNS解決失敗は即時失敗（リトライしても解決しないため）、
    429・5xxおよびその他のネットワークエラーは指数バックオフで最大max_retries回
    リトライする（ir-disclosure-dl/mufg-corporate-actionsと同じ方針）。"""
    params = {"date": date_str}
    if pagination_key:
        params["pagination_key"] = pagination_key
    url = f"{base_url}?{urllib.parse.urlencode(params)}"

    attempt = 0
    while True:
        attempt += 1
        request = urllib.request.Request(url, headers={"x-api-key": api_key})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
            result: dict[str, Any] = json.loads(body)
            return result
        except urllib.error.HTTPError as e:
            if e.code == 429 or 500 <= e.code < 600:
                if attempt > max_retries:
                    raise RuntimeError(f"{url}: リトライ上限に達しました (status={e.code})") from e
                time.sleep(min(60, 2**attempt))
                continue
            detail = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{url}: HTTPエラー status={e.code} {detail}") from e
        except urllib.error.URLError as e:
            if isinstance(e.reason, socket.gaierror):
                raise RuntimeError(f"{url}: 名前解決に失敗しました ({e.reason})") from e
            if attempt > max_retries:
                raise RuntimeError(f"{url}: ネットワークエラーが続くためリトライ上限に達しました ({e})") from e
            time.sleep(min(60, 2**attempt))


def fetch_endpoint(base_url: str, api_key: str, date_str: str) -> dict[str, Any]:
    """指定日の全銘柄分を取得する。pagination_keyが返る限りページを辿って
    dataを連結する（実機確認では出現しなかったが、将来の件数増加に備える）。"""
    all_data: list[Any] = []
    pagination_key: str | None = None
    while True:
        page = _fetch_page(base_url, api_key, date_str, pagination_key)
        data = page.get("data", [])
        assert isinstance(data, list)
        all_data.extend(data)
        next_key = page.get("pagination_key")
        pagination_key = next_key if isinstance(next_key, str) else None
        if not pagination_key:
            break
    return {"data": all_data}


def save_atomic(path: Path, data: bytes) -> None:
    """一時ファイル→renameでアトミックに保存する（他サービスと同じ方針）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / (path.name + ".tmp")
    tmp_path.write_bytes(data)
    os.replace(tmp_path, path)


def date_hierarchy_dir(base_dir: Path, date_str: str) -> Path:
    """日付文字列(YYYY-MM-DD)をyyyy/mm/ddの3階層ディレクトリに分解する
    （edinet-dl/jpx-daily-pdf-dlと同じレイク配置規約）。"""
    year, month, day = date_str.split("-")
    return base_dir / year / month / day


def endpoint_path(data_dir: Path, date_str: str, endpoint_name: str) -> Path:
    return date_hierarchy_dir(data_dir, date_str) / f"{endpoint_name}.json"


def recent_dates(end: datetime.date, days_window: int) -> list[str]:
    """endから過去days_window日分（end当日を含む）の日付文字列一覧を新しい順で返す。"""
    return [(end - datetime.timedelta(days=i)).isoformat() for i in range(days_window)]


def fetch_one(
    api_key: str, data_dir: Path, date_str: str, endpoint_name: str, base_url: str, logger: logging.Logger, force: bool
) -> tuple[bool, int]:
    """1日×1エンドポイント分を取得する。戻り値は (成功したか, 件数(スキップ時は-1))。"""
    dest = endpoint_path(data_dir, date_str, endpoint_name)
    if not force and dest.exists():
        return True, -1

    try:
        result = fetch_endpoint(base_url, api_key, date_str.replace("-", ""))
        body = json.dumps(result, ensure_ascii=False).encode("utf-8")
        save_atomic(dest, body)
        count = len(result["data"])
        logger.info(f"{date_str} ({endpoint_name}): 取得成功 ({count}件)")
        return True, count
    except Exception as e:
        logger.error(f"{date_str} ({endpoint_name}): 取得失敗 ({e})")
        return False, 0


def build_slack_message(results: dict[tuple[str, str], tuple[bool, int]]) -> str:
    failed = [key for key, (ok, _) in results.items() if not ok]
    if failed:
        lines = [f"❌ jquants-dl 日次実行 失敗 ({len(failed)}/{len(results)}件)"]
    else:
        lines = ["✅ jquants-dl 日次実行 成功"]

    for (date_str, endpoint_name), (ok, count) in sorted(results.items()):
        if not ok:
            lines.append(f"{date_str} ({endpoint_name}): NG")
        elif count == -1:
            lines.append(f"{date_str} ({endpoint_name}): スキップ(既存)")
        else:
            lines.append(f"{date_str} ({endpoint_name}): {count}件")
    return "\n".join(lines)


def send_slack_notification(webhook_url: str, message: str, logger: logging.Logger) -> None:
    """Slackへの通知失敗はログに記録するのみで、例外は上げない
    （ジョブ全体の成否に影響させない。他サービスと同じ設計）。"""
    try:
        payload = json.dumps({"text": message, "unfurl_links": False, "unfurl_media": False}).encode("utf-8")
        req = urllib.request.Request(
            webhook_url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            resp.read()
    except Exception as e:
        logger.error(f"Slack通知の送信に失敗しました: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", type=str, help="指定日のみ取得(YYYY-MM-DD)。省略時は直近DAYS_WINDOW日分")
    parser.add_argument("--force", action="store_true", help="既に取得済みの日も再取得する")
    args = parser.parse_args()

    data_dir = Path(os.environ.get("DATA_DIR", DEFAULT_DATA_DIR))
    log_path = os.environ.get("LOG_PATH", DEFAULT_LOG_PATH)
    days_window = int(os.environ.get("DAYS_WINDOW", DEFAULT_DAYS_WINDOW))
    slack_webhook_url = os.environ.get("SLACK_WEBHOOK_URL")

    logger = setup_logger(log_path)
    api_key = load_api_key()
    data_dir.mkdir(parents=True, exist_ok=True)

    # 実行時点の前日を起点にする（04時台に実行する前提で、前日分までが取引完了済み）。
    target_dates = [args.date] if args.date else recent_dates(today_jst() - datetime.timedelta(days=1), days_window)

    results: dict[tuple[str, str], tuple[bool, int]] = {}
    for date_str in target_dates:
        for endpoint_name, base_url in ENDPOINTS.items():
            results[(date_str, endpoint_name)] = fetch_one(
                api_key, data_dir, date_str, endpoint_name, base_url, logger, args.force
            )

    summary = build_slack_message(results)
    logger.info(summary.replace("\n", " / "))

    if slack_webhook_url:
        send_slack_notification(slack_webhook_url, summary, logger)

    if not all(ok for ok, _ in results.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()

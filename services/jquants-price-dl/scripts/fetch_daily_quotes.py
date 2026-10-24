#!/usr/bin/env python3
"""J-Quants API (https://api.jquants.com) の株価四本値エンドポイント
(GET /v2/equities/bars/daily) を日次で呼び出し、生レスポンスをそのまま保存する。

dateのみを指定すると、その日の全上場銘柄分（実機確認、2026-10-20: 4,443件、
ETF/REIT/インフラファンド等含む）が1レスポンスで返る（実機確認、pagination_key
は出現しなかったが、将来の仕様変更・件数増加に備えてページネーションには対応する）。
銘柄の絞り込みはしない（フィルタ・解釈は後段finance-dwh側の責務、レイク層は
生データをそのまま保存するという`edinet-dl`等と同じ方針）。

東証日報PDF（`jpx-daily-pdf-dl`）とは独立に収集する（2026-10-24決定）。日報は
前場/後場別の明細を持つがjQuantsには無く、jQuantsは株式分割等の調整後値
（AdjO/AdjH/AdjL/AdjC/AdjVo）を持つが日報には無い。両者は完全な重複ではなく、
同じ取引日のデータが両ソースに存在する期間は相互検証にも使える。取捨選択は
mart層以降の責務とし、cleansed層までは独立したテーブルのまま持つ。

取得対象は「直近DAYS_WINDOW日（既定3日、`edinet-dl`と同じ既定値）」のうち、
まだファイルが存在しない日のみ（`edinet-dl`と同じローリングウィンドウ+冪等
スキップ方式。実行し忘れた日があっても後続の実行で自動的に追いつける）。
1日1ファイルの単純な構成のため、進捗管理用のDBは持たない
（ファイルの存在チェックのみで十分、`jquants-validate/fetch_all.py`と同じ判断）。

設定は環境変数から読む(Dockerの --env-file を想定):
    JQUANTS_API_KEY  必須。J-Quantsダッシュボードで発行したAPIキー（V2、静的キー方式）
    DATA_DIR         省略時 /data/raw
    LOG_PATH         省略時 /data/logs/jquants-price-dl.log
    DAYS_WINDOW      省略時 3
    SLACK_WEBHOOK_URL 省略可。設定時のみ実行結果をSlackへ通知する

Usage:
    python3 fetch_daily_quotes.py                  # 直近DAYS_WINDOW日分（未取得のみ）
    python3 fetch_daily_quotes.py --date 2026-10-20 # 指定日のみ取得（手動補完用）
    python3 fetch_daily_quotes.py --force           # 既に取得済みの日も再取得する
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

BASE_URL = "https://api.jquants.com/v2/equities/bars/daily"
DEFAULT_DATA_DIR = "/data/raw"
DEFAULT_LOG_PATH = "/data/logs/jquants-price-dl.log"
DEFAULT_DAYS_WINDOW = 3
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5


def today_jst() -> datetime.date:
    return datetime.datetime.now(JST).date()


def setup_logger(log_path: str) -> logging.Logger:
    logger = logging.getLogger("jquants-price-dl")
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
    api_key: str, date_str: str, pagination_key: str | None, max_retries: int = 5
) -> dict[str, Any]:
    """1ページ分を取得する。DNS解決失敗は即時失敗（リトライしても解決しないため）、
    429・5xxおよびその他のネットワークエラーは指数バックオフで最大max_retries回
    リトライする（ir-disclosure-dl/mufg-corporate-actionsと同じ方針）。"""
    params = {"date": date_str}
    if pagination_key:
        params["pagination_key"] = pagination_key
    url = f"{BASE_URL}?{urllib.parse.urlencode(params)}"

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


def fetch_daily_quotes(api_key: str, date_str: str) -> dict[str, Any]:
    """指定日の全銘柄分を取得する。pagination_keyが返る限りページを辿って
    dataを連結する（実機確認では出現しなかったが、将来の件数増加に備える）。"""
    all_data: list[Any] = []
    pagination_key: str | None = None
    while True:
        page = _fetch_page(api_key, date_str, pagination_key)
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


def quotes_path(data_dir: Path, date_str: str) -> Path:
    return date_hierarchy_dir(data_dir, date_str) / "daily_quotes.json"


def recent_dates(end: datetime.date, days_window: int) -> list[str]:
    """endから過去days_window日分（end当日を含む）の日付文字列一覧を新しい順で返す。"""
    return [(end - datetime.timedelta(days=i)).isoformat() for i in range(days_window)]


def fetch_one(
    api_key: str, data_dir: Path, date_str: str, logger: logging.Logger, force: bool
) -> tuple[bool, int]:
    """1日分を取得する。戻り値は (成功したか, 件数(スキップ時は-1))。"""
    dest = quotes_path(data_dir, date_str)
    if not force and dest.exists():
        return True, -1

    try:
        result = fetch_daily_quotes(api_key, date_str.replace("-", ""))
        body = json.dumps(result, ensure_ascii=False).encode("utf-8")
        save_atomic(dest, body)
        count = len(result["data"])
        logger.info(f"{date_str}: 取得成功 ({count}件)")
        return True, count
    except Exception as e:
        logger.error(f"{date_str}: 取得失敗 ({e})")
        return False, 0


def build_slack_message(results: dict[str, tuple[bool, int]]) -> str:
    failed = [date_str for date_str, (ok, _) in results.items() if not ok]
    if failed:
        lines = [f"❌ jquants-price-dl 日次実行 失敗 ({len(failed)}/{len(results)}日)"]
    else:
        lines = ["✅ jquants-price-dl 日次実行 成功"]

    for date_str, (ok, count) in sorted(results.items()):
        if not ok:
            lines.append(f"{date_str}: NG")
        elif count == -1:
            lines.append(f"{date_str}: スキップ(既存)")
        else:
            lines.append(f"{date_str}: {count}件")
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

    results: dict[str, tuple[bool, int]] = {}
    for date_str in target_dates:
        results[date_str] = fetch_one(api_key, data_dir, date_str, logger, args.force)

    summary = build_slack_message(results)
    logger.info(summary.replace("\n", " / "))

    if slack_webhook_url:
        send_slack_notification(slack_webhook_url, summary, logger)

    if not all(ok for ok, _ in results.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()

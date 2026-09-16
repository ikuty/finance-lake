#!/usr/bin/env python3
"""三菱UFJ eスマート証券（kabu.com、旧auカブコム証券）が公開する株式分割・株式併合・
商号変更ページを週次で取得し、レンダリング後のHTMLをそのまま保存する。

個人利用限定（kabu.com利用規約「投資情報に関するご注意事項」
https://kabu.com/info/investment_advisory.html により、東証等の情報提供元データの
商用利用・第三者への提供目的での加工/再利用/再配信は不可。詳細はCLAUDE.md参照）。

**単純なHTTP GETでは取得できない**（2026-09-16実機確認）。対象3ページはテーブルの
実データを`<script src="/process/{name}.js">`が指す別ファイル内で
`document.write()`により描画しており、素のHTML（GET直後のレスポンス）には
`<div id="main">`と空のscriptタグしか含まれない。この.jsファイル自体は外部への
追加問い合わせ無しに完結した自己完結ファイル（実データが文字列リテラル・変数として
埋め込まれている）だが、`document.write()`呼び出しの内部実装（比率計算用の
一時変数等）に直接依存したパーサーを書くと、kabu.com側の描画ロジックが変わる
たびに壊れる。そのため**Playwrightでheadless Chromiumにより実際にレンダリングし、
最終的な完成後HTML（<table>が実データで埋まった状態）を保存する**方針とした
（2026-09-16決定。DWH側のパーサーは標準的な<table>構造にのみ依存すればよくなる）。
このリポジトリで初めてPlaywright/Chromiumを導入するが、今後もJavaScript描画に
依存するサイトからの取得が発生する見込みのため、汎用的な基盤として位置づける。

3ページ（株式分割・株式併合・商号変更）はいずれも「その時点での全履歴＋今後の予定」を
1ページに再掲載する形式で、EDINET/JPXのような日付ごとの独立ファイルではない
（実機確認、2026-09-16: 3ページとも2002年8月まで遡る数千行の表を毎回まるごと
再掲載している）。そのため「バックフィル」という概念が存在せず、週次で最新の
全量スナップショットを1回取得すれば足りる。バックフィル進捗レポート（年月×日の
マス目表示）もこの理由で実装しない。

Usage:
    python3 fetch_corporate_actions.py                # 今日1日分（週次実行前提）を取得
    python3 fetch_corporate_actions.py --force         # 既に取得済みでも再取得する

設定は環境変数から読む（Dockerの --env-file を想定）:
    DB_PATH              省略時 /data/index.db
    DATA_DIR             省略時 /data/raw
    LOG_PATH             省略時 /data/logs/mufg-corporate-actions.log
    SLACK_WEBHOOK_URL    省略可。設定時のみ実行結果をSlackへ通知する
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import sqlite3
import sys
import time
import urllib.request
from logging.handlers import RotatingFileHandler
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

JST = datetime.timezone(datetime.timedelta(hours=9))

DEFAULT_DB_PATH = "/data/index.db"
DEFAULT_DATA_DIR = "/data/raw"
DEFAULT_LOG_PATH = "/data/logs/mufg-corporate-actions.log"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5

BASE_HOST = "kabu.com"
USER_AGENT = "Mozilla/5.0 (compatible; mufg-corporate-actions-dl/1.0)"

# format名 -> URLパス。ファイル名にもそのままこのformat名を使う。
PAGES: dict[str, str] = {
    "bunkatu": "/investment/meigara/bunkatu.html",
    "gensi": "/investment/meigara/gensi.html",
    "syougou_henkou": "/investment/meigara/syougou_henkou.html",
}

FORMAT_LABELS: dict[str, str] = {
    "bunkatu": "株式分割",
    "gensi": "株式併合",
    "syougou_henkou": "商号変更",
}


def today_jst() -> datetime.date:
    return datetime.datetime.now(JST).date()


def setup_logger(log_path: str) -> logging.Logger:
    logger = logging.getLogger("mufg-corporate-actions")
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


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fetch_progress (
            period TEXT,      -- 'YYYY-MM-DD'（取得を実行した日。週次のため通常は月曜日）
            format TEXT,      -- 'bunkatu' | 'gensi' | 'syougou_henkou'
            status TEXT,      -- 'done' | 'error'
            sourceUrl TEXT,
            message TEXT,
            fetchedAt TEXT,
            PRIMARY KEY (period, format)
        )
    """)
    conn.commit()
    return conn


def already_done(conn: sqlite3.Connection, period: str, fmt: str) -> bool:
    row = conn.execute(
        "SELECT status FROM fetch_progress WHERE period = ? AND format = ?", (period, fmt)
    ).fetchone()
    return row is not None and row[0] == "done"


def store_progress(
    conn: sqlite3.Connection, period: str, fmt: str, status: str, source_url: str | None, message: str | None
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO fetch_progress (period, format, status, sourceUrl, message, fetchedAt) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (period, fmt, status, source_url, message, datetime.datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()


def _render_html(page: Page, url: str, max_retries: int = 5) -> str:
    """PlaywrightでURLを開き、JavaScript実行後(document.write完了後)の完全なHTML
    を返す。<script src>によるdocument.writeはパーサーをブロックする同期実行の
    ため、Playwrightの既定の遷移待ち（load イベント）で描画完了まで待てる
    （2026-09-16実機確認、追加のセレクタ待機は不要）。タイムアウト・ネットワーク
    エラーは最大5回指数バックオフでリトライする
    （jpx-daily-pdf-dl等の_http_getと同じ方針）。"""
    attempt = 0
    while True:
        attempt += 1
        try:
            page.goto(url, timeout=30_000)
            return page.content()
        except PlaywrightError as e:
            if attempt > max_retries:
                raise RuntimeError(f"{url}: リトライ上限に達しました ({e})") from e
            time.sleep(min(60, 2**attempt))


def save_atomic(path: Path, data: bytes) -> None:
    """一時ファイル→renameでアトミックに保存する。中断時に壊れたファイルが残らないようにする。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / (path.name + ".tmp")
    tmp_path.write_bytes(data)
    os.replace(tmp_path, path)


def date_hierarchy_dir(base_dir: Path, date_str: str) -> Path:
    """日付文字列(YYYY-MM-DD)をyyyy/mm/ddの3階層ディレクトリに分解する
    （edinet-dl/jpx-daily-pdf-dlと同じ理由）。"""
    year, month, day = date_str.split("-")
    return base_dir / year / month / day


def page_path(data_dir: Path, date_str: str, fmt: str) -> Path:
    return date_hierarchy_dir(data_dir, date_str) / f"{fmt}.html"


def fetch_one(
    conn: sqlite3.Connection,
    data_dir: Path,
    date_str: str,
    fmt: str,
    logger: logging.Logger,
    force: bool,
    page: Page,
) -> tuple[bool, int]:
    """1ページぶんを取得する。戻り値は (成功したか, ダウンロードバイト数(スキップ時は0))。"""
    if not force and already_done(conn, date_str, fmt):
        return True, 0

    rel_path = PAGES[fmt]
    url = f"https://{BASE_HOST}{rel_path}"
    dest = page_path(data_dir, date_str, fmt)
    try:
        html = _render_html(page, url)
        body = html.encode("utf-8")
        save_atomic(dest, body)
        store_progress(conn, date_str, fmt, "done", url, None)
        logger.info(f"{date_str} ({fmt}): 取得成功 ({len(body)}バイト)")
        return True, len(body)
    except Exception as e:
        store_progress(conn, date_str, fmt, "error", url, str(e))
        logger.error(f"{date_str} ({fmt}): 取得失敗 ({e})")
        return False, 0


def build_slack_message(date_str: str, results: dict[str, bool], downloaded_bytes: int) -> str:
    failed = [fmt for fmt, ok in results.items() if not ok]
    if failed:
        lines = [f"❌ mufg-corporate-actions 週次実行 失敗 ({len(failed)}/{len(results)}件)"]
    else:
        lines = ["✅ mufg-corporate-actions 週次実行 成功"]

    for fmt, ok in results.items():
        mark = "OK" if ok else "NG"
        lines.append(f"{FORMAT_LABELS[fmt]}: {mark}")

    mb = downloaded_bytes / (1024 * 1024)
    lines.append(f"対象日: {date_str} / ダウンロード: {mb:.2f}MB")
    return "\n".join(lines)


def send_slack_notification(webhook_url: str, message: str, logger: logging.Logger) -> None:
    """Slackへの通知失敗はログに記録するのみで、例外は上げない（ジョブ全体の成否に
    影響させない。edinet-dl/jpx-daily-pdf-dlと同じ設計）。"""
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
    parser.add_argument(
        "--force", action="store_true", help="既にdoneなページも対象に含める（既存ファイルは上書きされる）"
    )
    args = parser.parse_args()

    db_path = Path(os.environ.get("DB_PATH", DEFAULT_DB_PATH))
    data_dir = Path(os.environ.get("DATA_DIR", DEFAULT_DATA_DIR))
    log_path = os.environ.get("LOG_PATH", DEFAULT_LOG_PATH)
    slack_webhook_url = os.environ.get("SLACK_WEBHOOK_URL")

    logger = setup_logger(log_path)
    conn = init_db(db_path)
    data_dir.mkdir(parents=True, exist_ok=True)

    date_str = today_jst().isoformat()
    results: dict[str, bool] = {}
    downloaded_bytes = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            for fmt in PAGES:
                ok, n_bytes = fetch_one(conn, data_dir, date_str, fmt, logger, args.force, page)
                results[fmt] = ok
                downloaded_bytes += n_bytes
        finally:
            browser.close()

    summary = build_slack_message(date_str, results, downloaded_bytes)
    logger.info(summary.replace("\n", " / "))

    if slack_webhook_url:
        send_slack_notification(slack_webhook_url, summary, logger)

    if not all(results.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()

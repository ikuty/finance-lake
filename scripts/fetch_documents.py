#!/usr/bin/env python3
"""EDINET書類一覧API (https://api.edinet-fsa.go.jp/api/v2/documents.json) を
日付単位で呼び出し、書類本体（XBRL/PDF/CSV）を取得して生データのまま保存する。
取得状況は日付ごとにSQLite(fetch_progress)へ記録する。

このリポジトリはファイルの取得・格納（レイク層）に特化する。書類メタデータの
列展開・検索用インデックス作成、及び中身の解釈（パース）は後段の別リポジトリの
責務とし、ここでは扱わない。設計の詳細は docs/file_download_design.md を参照。

実行モードは2通り:
  - 初回バックフィル(手動): --start-date/--end-date で過去10年分程度を指定
  - 日次更新(cron): --daysを省略（環境変数DAYS_WINDOW、既定3日分の遡り窓で実行）

中断・再開可能な設計(fetch_progressテーブルで日付ごとの取得済み状態を記録)。--daysの
窓を1日より広く取ることで、電源障害等でジョブが実行されなかった日や、一時的な失敗で
errorになった日も、後続の実行で自動的に再試行される。

対象は secCode が設定されている書類（上場企業）のみ。type=1(XBRL)・type=2(PDF)・
type=5(CSV化XBRL)の3形式を取得する。XBRL・CSVはEDINETからzip形式で返るが、DWH等
での後利用を考慮し、展開した上で中身の各ファイルを個別にgzip圧縮して
data/{fileDate}/{edinetCode}/{docID}_{type}/ 配下に保存する（PDFは元々zipでは
ないため単一ファイルのまま data/{fileDate}/{edinetCode}/{docID}_pdf.pdf に保存）。
個々のファイル（PDF）・ディレクトリ（XBRL/CSV）は存在チェックによる冪等性を持ち、
DBに進捗テーブルは持たない（fetch_progressは日付単位のみ）。

Usage:
    python3 fetch_documents.py                    # DAYS_WINDOW日分（既定3日）を対象に日次実行
    python3 fetch_documents.py --start-date 2016-08-13 --end-date 2026-08-13
    python3 fetch_documents.py --days 7 --force   # 取得済みの日付も再取得

設定は環境変数から読む(Dockerの --env-file を想定):
    EDINET_API_KEY    必須。EDINET APIキー
    DB_PATH           省略時 /data/edinet_index.db
    DATA_DIR          省略時 /data/raw。書類本体の保存先ルート
    LOG_PATH          省略時 /data/logs/edinet-dl.log
    REQUEST_DELAY     省略時 1.2 (秒)
    DAYS_WINDOW       省略時 3。--days未指定時に日次実行で遡る日数
    SLACK_WEBHOOK_URL 省略可。設定時のみ実行結果をSlackへ通知する
"""
from __future__ import annotations

import argparse
import datetime
import gzip
import io
import json
import logging
import os
import shutil
import socket
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Iterator

DEFAULT_DB_PATH = "/data/edinet_index.db"
DEFAULT_DATA_DIR = "/data/raw"
DEFAULT_LOG_PATH = "/data/logs/edinet-dl.log"
DEFAULT_DELAY = 1.2
DEFAULT_DAYS_WINDOW = 3
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5MB
LOG_BACKUP_COUNT = 5  # 最大5世代 ≒ 合計25MB程度
PROGRESS_LOG_INTERVAL_DOCS = 20  # 日内の処理進捗ログを出す間隔（件数）
PROGRESS_LOG_INTERVAL_SECONDS = 30.0  # 日内の処理進捗ログを出す間隔（秒、いずれか早い方）

LIST_API_BASE = "https://api.edinet-fsa.go.jp/api/v2/documents.json"
DOC_API_BASE = "https://api.edinet-fsa.go.jp/api/v2/documents"
USER_AGENT = "Mozilla/5.0 (compatible; edinet-dl/1.0)"

# 書類取得APIのtype番号 -> ファイル名/ディレクトリ名のサフィックス
TYPE_SUFFIX: dict[int, str] = {1: "xbrl", 2: "pdf", 5: "csv"}
# zip形式で返るtype（展開して中身を個別にgzip圧縮する対象）。type=2(PDF)は元々zipでない。
ARCHIVE_TYPES = {1, 5}
# 展開時にzip内部のパスから取り除く接頭辞。CSV(type=5)は常に"XBRL_TO_CSV/"という単一の
# ラッパーフォルダしか持たないため、フラットな構造にする（件数によらず常に除去する）。
# XBRL(type=1)は"XBRL/PublicDoc/"・"XBRL/AuditDoc/"という意味のある階層を持つため除去しない。
ARCHIVE_STRIP_PREFIX: dict[int, str] = {5: "XBRL_TO_CSV/"}
# 書類一覧APIのフラグ項目名 -> 対応する書類取得APIのtype番号
FLAG_TYPE: list[tuple[str, int]] = [
    ("xbrlFlag", 1),
    ("pdfFlag", 2),
    ("csvFlag", 5),
]


class RateLimitedError(RuntimeError):
    pass


@dataclass
class RunStats:
    """1回の実行のサマリ。Slack通知用に集計するだけの一時的な構造で、永続化はしない。"""

    start: datetime.date
    end: datetime.date
    days_processed: list[str] = field(default_factory=list)
    days_failed: dict[str, str] = field(default_factory=dict)
    downloaded_count: int = 0
    downloaded_bytes: int = 0
    rate_limit_retries: int = 0


def force_ipv4() -> None:
    """socket.getaddrinfoの名前解決結果をIPv4のみに限定する。IPv6のRouter Advertisement
    ベースの経路がまれに一時的に消える現象（ENETUNREACH、2026-08-27・28に実機で複数回観測）
    を回避するための対策（2026-08-28導入）。stdlibのみで完結する標準的な手法。"""
    original_getaddrinfo = socket.getaddrinfo

    def getaddrinfo_ipv4_only(
        host: Any, port: Any, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0
    ) -> Any:
        return original_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = getaddrinfo_ipv4_only


def load_api_key() -> str:
    key = os.environ.get("EDINET_API_KEY")
    if not key:
        raise RuntimeError("環境変数 EDINET_API_KEY が設定されていません")
    return key


def setup_logger(log_path: str) -> logging.Logger:
    logger = logging.getLogger("edinet-dl")
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


def store_progress(
    conn: sqlite3.Connection, date_str: str, status: str, doc_count: int, message: str | None
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO fetch_progress (fileDate, status, docCount, message, fetchedAt) "
        "VALUES (?, ?, ?, ?, ?)",
        (date_str, status, doc_count, message, datetime.datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()


def _http_get(url: str, stats: RunStats, max_retries: int = 5) -> bytes:
    """共通のHTTPフェッチ+リトライ。429・ネットワークエラー/タイムアウト・5xxはリトライ対象
    （最大5回、指数バックオフ）、それ以外の4xxは即座に失敗とする（再試行しても無駄なため）。"""
    attempt = 0
    while True:
        attempt += 1
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body: bytes = resp.read()
                return body
        except urllib.error.HTTPError as e:
            retryable = e.code == 429 or 500 <= e.code < 600
            if e.code == 429:
                stats.rate_limit_retries += 1
            if not retryable:
                raise RuntimeError(f"{url}: HTTPエラー status={e.code}") from e
            if attempt > max_retries:
                raise RateLimitedError(f"{url}: リトライ上限に達しました (status={e.code})") from e
            time.sleep(min(60, 2**attempt))
        except (urllib.error.URLError, OSError) as e:
            if attempt > max_retries:
                raise RuntimeError(f"{url}: ネットワークエラーが続くためリトライ上限に達しました ({e})") from e
            time.sleep(min(60, 2**attempt))


def fetch_day(date_str: str, api_key: str, stats: RunStats, max_retries: int = 5) -> dict[str, Any]:
    url = f"{LIST_API_BASE}?date={date_str}&type=2&Subscription-Key={api_key}"
    attempt = 0
    while True:
        attempt += 1
        body = _http_get(url, stats, max_retries=max_retries)
        data: dict[str, Any] = json.loads(body.decode("utf-8"))
        status = str(data.get("metadata", {}).get("status", "200"))

        if status == "429":
            stats.rate_limit_retries += 1
            if attempt > max_retries:
                raise RateLimitedError(f"{date_str}: 429が続くためリトライ上限に達しました")
            time.sleep(min(60, 2**attempt))
            continue

        if status not in ("200", "OK"):
            message = data.get("metadata", {}).get("message", "unknown error")
            raise RuntimeError(f"{date_str}: EDINET APIエラー status={status} message={message}")

        return data


def fetch_document_file(
    doc_id: str, type_code: int, api_key: str, stats: RunStats, max_retries: int = 5
) -> bytes:
    url = f"{DOC_API_BASE}/{doc_id}?type={type_code}&Subscription-Key={api_key}"
    return _http_get(url, stats, max_retries=max_retries)


def doc_output_path(data_dir: Path, file_date: str, edinet_code: str, doc_id: str, type_code: int) -> Path:
    """type別のディレクトリ（xbrl/csv/pdf）を最上位にし、その下にdocIDで分ける。
    type=1(XBRL)/5(CSV)は展開後の格納先ディレクトリ（{type}/{docID}/）、type=2(PDF)は
    単一ファイルのパス（{type}/{docID}.pdf）を返す。docID階層を挟むのは、同一企業・同一日に
    複数docIDがある場合に、展開後のファイル名（manifest_PublicDoc.xml等）が衝突するのを
    防ぐため。"""
    type_dir = data_dir / file_date / edinet_code / TYPE_SUFFIX[type_code]
    if type_code in ARCHIVE_TYPES:
        return type_dir / doc_id
    return type_dir / f"{doc_id}.pdf"


def save_atomic(path: Path, data: bytes) -> None:
    """一時ファイル→renameでアトミックに保存する。中断時に壊れたファイルが残らないようにする。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / (path.name + ".tmp")
    tmp_path.write_bytes(data)
    os.replace(tmp_path, path)


def extract_and_gzip(zip_bytes: bytes, dest_dir: Path, strip_prefix: str = "") -> tuple[int, int]:
    """zipのバイト列を展開し、中身の各ファイルを個別にgzip圧縮してdest_dir配下に保存する
    （DWH等での後利用を考慮し、zipのまま保存せず展開・個別圧縮する）。strip_prefixを指定
    すると、zip内部のパスからその接頭辞を除去してから保存する（冗長なラッパーフォルダを
    フラット化するため）。一時ディレクトリに書き込んでからdest_dirへrenameすることで、
    中断・失敗時に不完全な状態が残らないようにする。戻り値は(展開したファイル数, 書き込んだ
    合計バイト数)。"""
    tmp_dir = dest_dir.parent / (dest_dir.name + ".tmp")
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)
    try:
        file_count = 0
        total_bytes = 0
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                data = zf.read(info.filename)
                name = info.filename
                if strip_prefix and name.startswith(strip_prefix):
                    name = name[len(strip_prefix):]
                out_path = tmp_dir / (name + ".gz")
                out_path.parent.mkdir(parents=True, exist_ok=True)
                with gzip.open(out_path, "wb") as f:
                    f.write(data)
                file_count += 1
                total_bytes += out_path.stat().st_size
        dest_dir.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp_dir, dest_dir)
        return file_count, total_bytes
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


def download_doc_files(
    doc: dict[str, Any],
    date_str: str,
    data_dir: Path,
    api_key: str,
    delay: float,
    stats: RunStats,
    logger: logging.Logger,
) -> bool:
    """対象docIDの、フラグが立っているtypeをダウンロードする。既に存在するファイルはスキップ
    する。全て成功（またはスキップ）すればTrue、1件でも失敗すればFalseを返す。"""
    doc_id = doc["docID"]
    edinet_code = doc["edinetCode"]
    ok = True
    for flag_name, type_code in FLAG_TYPE:
        if doc.get(flag_name) != "1":
            continue
        dest = doc_output_path(data_dir, date_str, edinet_code, doc_id, type_code)
        if dest.exists():
            continue
        try:
            body = fetch_document_file(doc_id, type_code, api_key, stats)
            if type_code in ARCHIVE_TYPES:
                strip_prefix = ARCHIVE_STRIP_PREFIX.get(type_code, "")
                file_count, written_bytes = extract_and_gzip(body, dest, strip_prefix=strip_prefix)
                stats.downloaded_count += file_count
                stats.downloaded_bytes += written_bytes
            else:
                save_atomic(dest, body)
                stats.downloaded_count += 1
                stats.downloaded_bytes += len(body)
        except Exception as e:
            ok = False
            logger.error(f"{date_str} {doc_id} edinetCode={edinet_code} type={type_code}: ダウンロード失敗 ({e})")
        finally:
            time.sleep(delay)
    return ok


def process_day(
    conn: sqlite3.Connection,
    date_str: str,
    api_key: str,
    data_dir: Path,
    delay: float,
    stats: RunStats,
    logger: logging.Logger,
    log_path: str,
) -> int:
    """対象日の一覧取得〜書類本体ダウンロード〜fetch_progress更新までを行う。
    戻り値はsecCode絞り込み後の対象件数。"""
    day_start = time.monotonic()
    data = fetch_day(date_str, api_key, stats)
    all_results = data.get("results", [])
    targets = [r for r in all_results if r.get("secCode")]
    logger.info(f"{date_str}: 一覧{len(all_results)}件 / 対象{len(targets)}件")

    failed_doc_ids: list[str] = []
    day_start_downloaded_count = stats.downloaded_count
    last_progress_at = time.monotonic()
    for i, doc in enumerate(targets, start=1):
        ok = download_doc_files(doc, date_str, data_dir, api_key, delay, stats, logger)
        if not ok:
            failed_doc_ids.append(doc["docID"])

        now = time.monotonic()
        if i % PROGRESS_LOG_INTERVAL_DOCS == 0 or now - last_progress_at >= PROGRESS_LOG_INTERVAL_SECONDS:
            files_so_far = stats.downloaded_count - day_start_downloaded_count
            logger.info(f"{date_str}: 進捗 {i}/{len(targets)}件処理済み（ダウンロード{files_so_far}ファイル）")
            last_progress_at = now

    elapsed = time.monotonic() - day_start
    logger.info(
        f"{date_str}: 完了 (成功{len(targets) - len(failed_doc_ids)}件 / "
        f"失敗{len(failed_doc_ids)}件, {elapsed:.1f}秒)"
    )

    if failed_doc_ids:
        message = f"{len(failed_doc_ids)}/{len(targets)} files failed (see {log_path})"
        store_progress(conn, date_str, "error", len(targets), message)
        stats.days_failed[date_str] = message
    else:
        store_progress(conn, date_str, "done", len(targets), None)

    stats.days_processed.append(date_str)
    return len(targets)


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
    data_dir: Path,
    logger: logging.Logger,
    log_path: str,
) -> RunStats:
    dates = list(date_range(start, end))
    todo = [d for d in dates if force or not already_done(conn, d.isoformat())]
    stats = RunStats(start=start, end=end)
    logger.info(f"対象期間: {start} 〜 {end}（{len(dates)}日間）/ 未取得: {len(todo)}日 / force={force}")

    for i, d in enumerate(todo):
        date_str = d.isoformat()
        try:
            process_day(conn, date_str, api_key, data_dir, delay, stats, logger, log_path)
        except RateLimitedError as e:
            logger.error(f"[{i + 1}/{len(todo)}] {date_str}: 中断 ({e})")
            break
        except Exception as e:
            store_progress(conn, date_str, "error", 0, str(e))
            stats.days_failed[date_str] = str(e)
            logger.error(f"[{i + 1}/{len(todo)}] {date_str}: エラー ({e})")

        if i < len(todo) - 1:
            time.sleep(delay)

    logger.info(
        f"完了。処理{len(stats.days_processed)}日 / "
        f"ダウンロード成功{stats.downloaded_count}件 / 失敗{len(stats.days_failed)}日"
    )
    return stats


def format_bytes(n: int) -> str:
    mb = n / (1024 * 1024)
    if mb >= 1024:
        return f"{mb / 1024:.1f}GB"
    return f"{mb:.1f}MB"


def build_slack_message(stats: RunStats, free_bytes: int) -> str:
    free_gb = free_bytes / (1024**3)
    period = f"{stats.start} 〜 {stats.end}"
    n_days = len(stats.days_processed)

    if stats.days_failed:
        n_failed = len(stats.days_failed)
        lines = [
            f"❌ edinet-dl 日次実行 失敗 ({n_failed}/{n_days}日)",
            f"期間: {period} ({n_days}日処理、うち{n_failed}日失敗)",
        ]
        for date_str, message in stats.days_failed.items():
            lines.append(f"失敗: {date_str} ({message})")
    else:
        lines = [
            "✅ edinet-dl 日次実行 成功",
            f"期間: {period} ({n_days}日処理)",
        ]

    lines.append(f"ダウンロード: {stats.downloaded_count}件 / {format_bytes(stats.downloaded_bytes)}")
    lines.append(f"429発生: {stats.rate_limit_retries}回")
    lines.append(f"空き容量: {free_gb:.1f}GB")
    return "\n".join(lines)


def send_slack_notification(webhook_url: str, message: str, logger: logging.Logger) -> None:
    """Slackへの通知失敗はログに記録するのみで、例外は上げない（ジョブ全体の成否に影響させない）。"""
    try:
        payload = json.dumps({"text": message}).encode("utf-8")
        req = urllib.request.Request(
            webhook_url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception as e:
        logger.error(f"Slack通知の送信に失敗しました: {e}")


def main() -> None:
    force_ipv4()
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
    data_dir = Path(os.environ.get("DATA_DIR", DEFAULT_DATA_DIR))
    log_path = os.environ.get("LOG_PATH", DEFAULT_LOG_PATH)
    delay = float(os.environ.get("REQUEST_DELAY", DEFAULT_DELAY))
    slack_webhook_url = os.environ.get("SLACK_WEBHOOK_URL")

    logger = setup_logger(log_path)
    conn = init_db(db_path)
    data_dir.mkdir(parents=True, exist_ok=True)

    stats = run(conn, api_key, start, end, delay, args.force, data_dir, logger, log_path)

    try:
        free_bytes = shutil.disk_usage(data_dir).free
    except OSError:
        free_bytes = 0

    message = build_slack_message(stats, free_bytes)
    logger.info("summary: " + message.replace("\n", " / "))

    if slack_webhook_url:
        send_slack_notification(slack_webhook_url, message, logger)


if __name__ == "__main__":
    main()

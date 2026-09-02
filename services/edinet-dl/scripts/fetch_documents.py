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

「今日」はJSTで評価する（`today_jst()`、2026-08-31修正）。edinet-dl.timerはJST 04:01:30
（=UTC前日19:01:30）に発火するため、`datetime.date.today()`（システムTZ依存、コンテナは
通常UTC）を使うとDAYS_WINDOWの対象期間が常に1日ずれてしまう。

DAYS_WINDOWの対象期間の終端は「今日」ではなく**前日**（`last_complete_day_jst()`、
2026-09-03修正）。日次ジョブは営業開始前のJST 04:01:30に実行されるため、「今日」を
対象に含めても一覧APIは常に0件を返す。この0件がfetch_progressに`done`として確定記録
されると、`already_done()`により当日の本当のデータ（その日の営業時間中に提出される分）
が翌日以降も二度と自動取得されなくなる不具合があった。

対象は secCode が設定されている書類（上場企業）のみ。type=1(XBRL)・type=2(PDF)・
type=5(CSV化XBRL)の3形式のうち、--csv/--pdf/--xbrlで指定したものだけを取得する
（いずれも未指定なら全形式が対象、後方互換のデフォルト）。XBRL・CSVはEDINETから
zip形式で返るが、DWH等での後利用を考慮し、展開した上で中身の各ファイルを個別に
gzip圧縮して data/{yyyy}/{mm}/{dd}/{edinetCode}/{type}/{docID}/ 配下に保存する
（PDFは元々zipではないため単一ファイルのまま
data/{yyyy}/{mm}/{dd}/{edinetCode}/pdf/{docID}.pdf に保存）。日付をyyyy/mm/ddの3階層に
分けるのは、1年365個・10年3650個のディレクトリがdata_dir直下にフラットに並ぶのを避ける
ため（2026-09-02決定）。個々のファイル（PDF）・ディレクトリ（XBRL/CSV）は存在チェックに
よる冪等性を持ち、DBに進捗テーブルは持たない（fetch_progressは日付単位のみ）。

書類一覧APIの生レスポンス（secCode等で絞り込む前の全件）も、日毎に1ファイル
data/response/{yyyy}/{mm}/{dd}/document_list.json として加工せず保存する。書類本体だけでは
失われるメタデータ（docTypeCode・filerName・submitDateTime等）を、後段がEDINET APIへ
再アクセスせずに参照できるようにするため。

EDINET APIへのHTTP接続はKeep-Aliveで使い回す（`EdinetHttpClient`、2026-09-01導入）。
1日あたり数百回同一ホストへ通信するため、リクエストごとの新規TCP+TLSハンドシェイクの
積み重ねが所要時間の大半を占めていた。

Usage:
    python3 fetch_documents.py                    # DAYS_WINDOW日分（既定3日）を対象に日次実行
    python3 fetch_documents.py --start-date 2016-08-13 --end-date 2026-08-13
    python3 fetch_documents.py --days 7 --force   # 取得済みの日付も再取得
    python3 fetch_documents.py --csv --pdf        # CSV・PDFのみ取得（XBRLは対象外）

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
import http.client
import io
import json
import logging
import os
import shutil
import socket
import sqlite3
import sys
import time
import urllib.request
import zipfile
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Iterator, Protocol

JST = datetime.timezone(datetime.timedelta(hours=9))

DEFAULT_DB_PATH = "/data/edinet_index.db"
DEFAULT_DATA_DIR = "/data/raw"
DEFAULT_LOG_PATH = "/data/logs/edinet-dl.log"
DEFAULT_DELAY = 1.2
DEFAULT_DAYS_WINDOW = 3
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5MB
LOG_BACKUP_COUNT = 5  # 最大5世代 ≒ 合計25MB程度
PROGRESS_LOG_INTERVAL_DOCS = 20  # 日内の処理進捗ログを出す間隔（件数）
PROGRESS_LOG_INTERVAL_SECONDS = 30.0  # 日内の処理進捗ログを出す間隔（秒、いずれか早い方）

API_HOST = "api.edinet-fsa.go.jp"
LIST_API_PATH = "/api/v2/documents.json"
DOC_API_PATH = "/api/v2/documents"
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


class _HTTPResponseLike(Protocol):
    """EdinetHttpClientが要求する最小限のレスポンスインターフェース
    （http.client.HTTPResponseの構造的部分型）。テストではこれを満たすフェイクに差し替える。"""

    status: int

    def read(self) -> bytes: ...


class _HTTPConnectionLike(Protocol):
    """EdinetHttpClientが要求する最小限の接続インターフェース
    （http.client.HTTPSConnectionの構造的部分型）。テストではこれを満たすフェイクに差し替える。"""

    def request(self, method: str, url: str, body: Any = None, headers: dict[str, str] = ...) -> None: ...
    def getresponse(self) -> _HTTPResponseLike: ...
    def close(self) -> None: ...


class EdinetHttpClient:
    """api.edinet-fsa.go.jpへのHTTP接続をKeep-Aliveで使い回すクライアント（2026-09-01導入）。
    urllib.request.urlopen()は呼び出しごとに新規にTCP+TLSハンドシェイクを行うが、本ジョブは
    1日あたり数百回同一ホストへ通信するため、接続確立コストの積み重ねが所要時間の大半を
    占めていた（2026-09-01計測: 1リクエストあたり0.1〜0.18秒のうち大半が接続確立コストと
    推定。詳細はdocs/file_download_design.md参照）。stdlibの http.client のみで完結する
    （新規依存なし）。同一実行内の一覧取得API・書類取得APIすべてで1本の接続を使い回す
    （Slack通知は別ホストのため対象外、従来通りurllib.requestを使う）。"""

    def __init__(self, host: str = API_HOST, timeout: float = 30) -> None:
        self._host = host
        self._timeout = timeout
        self._conn: _HTTPConnectionLike | None = None

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def get(self, path: str, stats: RunStats, max_retries: int = 5) -> bytes:
        """共通のHTTPフェッチ+リトライ。429・ネットワークエラー/タイムアウト・5xxはリトライ対象
        （最大5回、指数バックオフ）、それ以外の4xxは即座に失敗とする（再試行しても無駄なため）。
        接続がサーバ側のアイドルタイムアウト等で切れていた場合は、closeした上で次の試行時に
        自動的に再接続する。"""
        attempt = 0
        while True:
            attempt += 1
            try:
                if self._conn is None:
                    self._conn = http.client.HTTPSConnection(self._host, timeout=self._timeout)
                self._conn.request("GET", path, headers={"User-Agent": USER_AGENT})
                resp = self._conn.getresponse()
                body = resp.read()
            except (http.client.HTTPException, OSError) as e:
                self.close()
                if attempt > max_retries:
                    raise RuntimeError(f"{path}: ネットワークエラーが続くためリトライ上限に達しました ({e})") from e
                time.sleep(min(60, 2**attempt))
                continue

            if resp.status == 429 or 500 <= resp.status < 600:
                if resp.status == 429:
                    stats.rate_limit_retries += 1
                if attempt > max_retries:
                    raise RateLimitedError(f"{path}: リトライ上限に達しました (status={resp.status})")
                time.sleep(min(60, 2**attempt))
                continue

            if resp.status >= 400:
                raise RuntimeError(f"{path}: HTTPエラー status={resp.status}")

            return body


def fetch_day(
    client: EdinetHttpClient, date_str: str, api_key: str, stats: RunStats, max_retries: int = 5
) -> dict[str, Any]:
    path = f"{LIST_API_PATH}?date={date_str}&type=2&Subscription-Key={api_key}"
    attempt = 0
    while True:
        attempt += 1
        body = client.get(path, stats, max_retries=max_retries)
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
    client: EdinetHttpClient, doc_id: str, type_code: int, api_key: str, stats: RunStats, max_retries: int = 5
) -> bytes:
    path = f"{DOC_API_PATH}/{doc_id}?type={type_code}&Subscription-Key={api_key}"
    return client.get(path, stats, max_retries=max_retries)


def date_hierarchy_dir(base_dir: Path, file_date: str) -> Path:
    """日付文字列(YYYY-MM-DD)をyyyy/mm/ddの3階層ディレクトリに分解する。1年365個・
    10年3650個のディレクトリがbase_dir直下にフラットに並ぶのを避けるため
    （2026-09-02決定）。"""
    year, month, day = file_date.split("-")
    return base_dir / year / month / day


def list_response_path(data_dir: Path, file_date: str) -> Path:
    """書類一覧APIの生レスポンスの保存先。書類本体（{yyyy}/{mm}/{dd}/{edinetCode}/...)とは
    別の data_dir/response/ 配下にまとめる。日毎に1ファイル、後段が同APIを再度呼ばずに
    docID・edinetCode・secCode・filerName・docTypeCode等のメタデータへアクセスできるように
    するため（書類本体のみでは失われる情報）。"""
    return date_hierarchy_dir(data_dir / "response", file_date) / "document_list.json"


def save_list_response(data_dir: Path, file_date: str, data: dict[str, Any]) -> None:
    """一覧APIのレスポンス（secCode等で絞り込む前の全件）を加工せずJSONのまま保存する。
    日次実行のたびに最新の内容で上書きする（同日内に後から提出される書類もあるため）。"""
    path = list_response_path(data_dir, file_date)
    body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    save_atomic(path, body)


def doc_output_path(data_dir: Path, file_date: str, edinet_code: str, doc_id: str, type_code: int) -> Path:
    """type別のディレクトリ（xbrl/csv/pdf）を最上位にし、その下にdocIDで分ける。
    type=1(XBRL)/5(CSV)は展開後の格納先ディレクトリ（{type}/{docID}/）、type=2(PDF)は
    単一ファイルのパス（{type}/{docID}.pdf）を返す。docID階層を挟むのは、同一企業・同一日に
    複数docIDがある場合に、展開後のファイル名（manifest_PublicDoc.xml等）が衝突するのを
    防ぐため。"""
    type_dir = date_hierarchy_dir(data_dir, file_date) / edinet_code / TYPE_SUFFIX[type_code]
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
    client: EdinetHttpClient,
    doc: dict[str, Any],
    date_str: str,
    data_dir: Path,
    api_key: str,
    delay: float,
    stats: RunStats,
    logger: logging.Logger,
    enabled_types: set[int],
) -> bool:
    """対象docIDの、フラグが立っておりenabled_typesに含まれるtypeをダウンロードする。
    既に存在するファイルはスキップする。全て成功（またはスキップ）すればTrue、1件でも
    失敗すればFalseを返す。"""
    doc_id = doc["docID"]
    edinet_code = doc["edinetCode"]
    ok = True
    for flag_name, type_code in FLAG_TYPE:
        if type_code not in enabled_types:
            continue
        if doc.get(flag_name) != "1":
            continue
        dest = doc_output_path(data_dir, date_str, edinet_code, doc_id, type_code)
        if dest.exists():
            continue
        try:
            body = fetch_document_file(client, doc_id, type_code, api_key, stats)
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
    client: EdinetHttpClient,
    date_str: str,
    api_key: str,
    data_dir: Path,
    delay: float,
    stats: RunStats,
    logger: logging.Logger,
    log_path: str,
    enabled_types: set[int],
) -> int:
    """対象日の一覧取得〜書類本体ダウンロード〜fetch_progress更新までを行う。
    戻り値はsecCode絞り込み後の対象件数。"""
    day_start = time.monotonic()
    data = fetch_day(client, date_str, api_key, stats)
    save_list_response(data_dir, date_str, data)
    all_results = data.get("results", [])
    targets = [r for r in all_results if r.get("secCode")]
    logger.info(f"{date_str}: 一覧{len(all_results)}件 / 対象{len(targets)}件")

    failed_doc_ids: list[str] = []
    day_start_downloaded_count = stats.downloaded_count
    last_progress_at = time.monotonic()
    for i, doc in enumerate(targets, start=1):
        ok = download_doc_files(client, doc, date_str, data_dir, api_key, delay, stats, logger, enabled_types)
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


def today_jst() -> datetime.date:
    """JSTでの「今日」を返す。datetime.date.today()はコンテナのシステムTZ（通常UTC）に
    依存するため使わない。edinet-dl.timerはJST 04:01:30（=UTC前日19:01:30）に発火するため、
    UTCで日付を評価すると常にJSTより1日古い日付になり、DAYS_WINDOWの対象期間が1日ずれる
    （2026-08-31発見）。固定オフセットで計算するため、tzdataパッケージは不要。"""
    return datetime.datetime.now(JST).date()


def last_complete_day_jst() -> datetime.date:
    """取得対象として安全な「最後の完結した日」＝前日を返す。日次ジョブはJST 04:01:30
    （その日の営業開始前）に実行されるため、「今日」を対象に含めても一覧APIは常に0件を
    返す。この0件が`fetch_progress`に`done`としてそのまま確定記録されてしまうと、
    `already_done()`により当日の本当のデータ（その日の営業時間中に提出される分）が
    翌日以降も二度と自動取得されなくなる（2026-09-03発見）。"""
    return today_jst() - datetime.timedelta(days=1)


def run(
    conn: sqlite3.Connection,
    client: EdinetHttpClient,
    api_key: str,
    start: datetime.date,
    end: datetime.date,
    delay: float,
    force: bool,
    data_dir: Path,
    logger: logging.Logger,
    log_path: str,
    enabled_types: set[int],
) -> RunStats:
    dates = list(date_range(start, end))
    todo = [d for d in dates if force or not already_done(conn, d.isoformat())]
    stats = RunStats(start=start, end=end)
    type_names = ",".join(TYPE_SUFFIX[t] for t in sorted(enabled_types))
    logger.info(
        f"対象期間: {start} 〜 {end}（{len(dates)}日間）/ 未取得: {len(todo)}日 / "
        f"force={force} / 対象type: {type_names}"
    )

    for i, d in enumerate(todo):
        date_str = d.isoformat()
        try:
            process_day(conn, client, date_str, api_key, data_dir, delay, stats, logger, log_path, enabled_types)
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


def compute_enabled_types(xbrl: bool, pdf: bool, csv: bool) -> set[int]:
    """--xbrl/--pdf/--csvのフラグから対象typeの集合を決める。いずれも未指定（すべてFalse）
    なら全形式が対象（後方互換のデフォルト）。1つでも指定されていれば、指定されたものだけが
    対象になる。"""
    if not (xbrl or pdf or csv):
        return {1, 2, 5}
    enabled_types: set[int] = set()
    if xbrl:
        enabled_types.add(1)
    if pdf:
        enabled_types.add(2)
    if csv:
        enabled_types.add(5)
    return enabled_types


def main() -> None:
    force_ipv4()
    default_days = int(os.environ.get("DAYS_WINDOW", DEFAULT_DAYS_WINDOW))
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--days", type=int, default=default_days,
        help=f"前日から遡って何日分を対象にするか（省略時は環境変数DAYS_WINDOW、既定{default_days}）",
    )
    parser.add_argument("--start-date", type=str, help="開始日 YYYY-MM-DD（指定時は--daysより優先）")
    parser.add_argument("--end-date", type=str, help="終了日 YYYY-MM-DD（省略時は前日）")
    parser.add_argument("--force", action="store_true", help="取得済みの日付も再取得する")
    parser.add_argument("--xbrl", action="store_true", help="XBRL(type=1)を対象にする")
    parser.add_argument("--pdf", action="store_true", help="PDF(type=2)を対象にする")
    parser.add_argument("--csv", action="store_true", help="CSV(type=5)を対象にする")
    args = parser.parse_args()

    enabled_types = compute_enabled_types(args.xbrl, args.pdf, args.csv)

    last_complete_day = last_complete_day_jst()
    if args.start_date:
        start = datetime.date.fromisoformat(args.start_date)
        end = datetime.date.fromisoformat(args.end_date) if args.end_date else last_complete_day
    elif args.days:
        end = last_complete_day
        start = last_complete_day - datetime.timedelta(days=args.days - 1)
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

    client = EdinetHttpClient()
    try:
        stats = run(conn, client, api_key, start, end, delay, args.force, data_dir, logger, log_path, enabled_types)
    finally:
        client.close()

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

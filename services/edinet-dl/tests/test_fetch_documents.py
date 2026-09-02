from __future__ import annotations

import datetime
import gzip
import http.client
import io
import json
import logging
import socket
import sys
import time
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fetch_documents  # noqa: E402

TEST_LOGGER = logging.getLogger("test-edinet-dl")


def make_response(status: str, results: list[dict[str, Any]]) -> bytes:
    return json.dumps({"metadata": {"status": status}, "results": results}).encode("utf-8")


def make_stats() -> fetch_documents.RunStats:
    return fetch_documents.RunStats(start=datetime.date(2026, 8, 13), end=datetime.date(2026, 8, 13))


ALL_TYPES = {1, 2, 5}


# --- force_ipv4 ---------------------------------------------------------------


def test_force_ipv4_always_requests_af_inet(monkeypatch: pytest.MonkeyPatch) -> None:
    requested_families = []

    def fake_getaddrinfo(
        host: object, port: object, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0
    ) -> list[object]:
        requested_families.append(family)
        return []

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    fetch_documents.force_ipv4()

    socket.getaddrinfo("example.com", 443, socket.AF_INET6)  # 呼び出し側がIPv6を指定しても
    socket.getaddrinfo("example.com", 443)  # 未指定でも

    assert requested_families == [socket.AF_INET, socket.AF_INET]


# --- date_range / DB progress ------------------------------------------------


def test_date_range_inclusive() -> None:
    start = datetime.date(2026, 1, 1)
    end = datetime.date(2026, 1, 3)
    assert list(fetch_documents.date_range(start, end)) == [
        datetime.date(2026, 1, 1),
        datetime.date(2026, 1, 2),
        datetime.date(2026, 1, 3),
    ]


def test_today_jst_uses_jst_not_system_tz(monkeypatch: pytest.MonkeyPatch) -> None:
    # edinet-dl.timerはJST 04:01:30に発火するが、これはUTCでは前日19:01:30。
    # date.today()（システムTZ依存、通常UTC）だと日付が1日古くなるバグを防ぐための確認。
    fixed_utc = datetime.datetime(2026, 8, 31, 19, 1, 52, tzinfo=datetime.timezone.utc)

    class FixedDatetime(datetime.datetime):
        @classmethod
        def now(cls, tz: datetime.tzinfo | None = None) -> datetime.datetime:  # type: ignore[override]
            return fixed_utc.astimezone(tz) if tz else fixed_utc

    # fetch_documents.pyもこのテストファイルも同じdatetimeモジュールをimportしているため、
    # モジュールオブジェクト自体を差し替えれば両方に反映される（fetch_documents.datetimeという
    # 属性アクセス経由だとmypy strictが「暗黙の再エクスポート」として拒否するため、直接importした
    # datetimeモジュールを操作する）。
    monkeypatch.setattr(datetime, "datetime", FixedDatetime)

    assert fetch_documents.today_jst() == datetime.date(2026, 9, 1)


def test_last_complete_day_jst_is_yesterday(monkeypatch: pytest.MonkeyPatch) -> None:
    # 日次ジョブは営業開始前のJST 04:01:30に実行されるため、「今日」を対象に含めても
    # 一覧APIは常に0件を返す。取得対象の終端は前日でなければならない（2026-09-03発見）。
    fixed_utc = datetime.datetime(2026, 9, 2, 19, 1, 52, tzinfo=datetime.timezone.utc)

    class FixedDatetime(datetime.datetime):
        @classmethod
        def now(cls, tz: datetime.tzinfo | None = None) -> datetime.datetime:  # type: ignore[override]
            return fixed_utc.astimezone(tz) if tz else fixed_utc

    monkeypatch.setattr(datetime, "datetime", FixedDatetime)

    assert fetch_documents.today_jst() == datetime.date(2026, 9, 3)
    assert fetch_documents.last_complete_day_jst() == datetime.date(2026, 9, 2)


def test_init_db_creates_tables(tmp_path: Path) -> None:
    conn = fetch_documents.init_db(tmp_path / "index.db")
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"fetch_progress"} <= tables


def test_store_progress_done_and_already_done(tmp_path: Path) -> None:
    conn = fetch_documents.init_db(tmp_path / "index.db")
    fetch_documents.store_progress(conn, "2026-08-13", "done", 5, None)
    assert fetch_documents.already_done(conn, "2026-08-13")

    row = conn.execute(
        "SELECT docCount, message FROM fetch_progress WHERE fileDate = ?", ("2026-08-13",)
    ).fetchone()
    assert row == (5, None)


def test_store_progress_error_is_not_done(tmp_path: Path) -> None:
    conn = fetch_documents.init_db(tmp_path / "index.db")
    fetch_documents.store_progress(conn, "2026-08-13", "error", 3, "2/3 files failed")
    assert not fetch_documents.already_done(conn, "2026-08-13")

    row = conn.execute(
        "SELECT status, message FROM fetch_progress WHERE fileDate = ?", ("2026-08-13",)
    ).fetchone()
    assert row == ("error", "2/3 files failed")


def test_store_progress_overwrites_on_rerun(tmp_path: Path) -> None:
    conn = fetch_documents.init_db(tmp_path / "index.db")
    fetch_documents.store_progress(conn, "2026-08-13", "error", 3, "boom")
    fetch_documents.store_progress(conn, "2026-08-13", "done", 3, None)

    rows = conn.execute(
        "SELECT status FROM fetch_progress WHERE fileDate = ?", ("2026-08-13",)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "done"


# --- EdinetHttpClient: Keep-Alive接続+共通のHTTPフェッチ・リトライ -----------------


class _FakeHTTPResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body


class _FakeHTTPSConnection:
    """http.client.HTTPSConnectionの最小限のフェイク。responsesに(status, body)または
    例外を順に並べておくと、requestのたびに順番に返す/送出する。呼ばれたpathの記録・
    closeされたかどうかの確認にも使う。"""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = responses
        self._index = 0
        self.requested_paths: list[str] = []
        self.closed = False

    def request(self, method: str, path: str, body: Any = None, headers: dict[str, str] = {}) -> None:
        self.requested_paths.append(path)

    def getresponse(self) -> _FakeHTTPResponse:
        item = self._responses[self._index]
        self._index += 1
        if isinstance(item, Exception):
            raise item
        status, body = item
        return _FakeHTTPResponse(status, body)

    def close(self) -> None:
        self.closed = True


def make_client(*responses: Any) -> fetch_documents.EdinetHttpClient:
    """responsesを返す（または例外を送出する）フェイク接続を、接続済みの状態で仕込んだ
    EdinetHttpClientを返す。呼び出し側で毎回新規接続されないことの確認にも使える。"""
    client = fetch_documents.EdinetHttpClient()
    client._conn = _FakeHTTPSConnection(list(responses))
    return client


def _connection_factory(*connections: Any) -> Any:
    """http.client.HTTPSConnectionの差し替え用。呼ばれるたびに渡された接続を順番に返す
    （再接続のシナリオをテストするため）。"""
    state = {"i": 0}

    def factory(host: str, timeout: float = 30) -> Any:
        conn = connections[state["i"]]
        state["i"] += 1
        return conn

    return factory


def test_edinet_http_client_reuses_same_connection_across_requests() -> None:
    stats = make_stats()
    client = make_client((200, b"first"), (200, b"second"))
    fake_conn = client._conn

    assert client.get("/a", stats) == b"first"
    assert client.get("/b", stats) == b"second"

    assert client._conn is fake_conn  # 接続が使い回されている
    assert isinstance(fake_conn, _FakeHTTPSConnection)
    assert fake_conn.requested_paths == ["/a", "/b"]


def test_edinet_http_client_retries_on_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    stats = make_stats()
    client = make_client((429, b""), (200, b"ok"))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    body = client.get("/x", stats)
    assert body == b"ok"
    assert stats.rate_limit_retries == 1


def test_edinet_http_client_retries_on_5xx_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    stats = make_stats()
    client = make_client((500, b""), (200, b"ok"))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    body = client.get("/x", stats)
    assert body == b"ok"


def test_edinet_http_client_fails_immediately_on_404(monkeypatch: pytest.MonkeyPatch) -> None:
    stats = make_stats()
    client = make_client((404, b""))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with pytest.raises(RuntimeError, match="404"):
        client.get("/x", stats)
    fake_conn = client._conn
    assert isinstance(fake_conn, _FakeHTTPSConnection)
    assert len(fake_conn.requested_paths) == 1  # リトライしない


def test_edinet_http_client_raises_rate_limited_after_max_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    stats = make_stats()
    client = make_client((429, b""), (429, b""), (429, b""))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with pytest.raises(fetch_documents.RateLimitedError):
        client.get("/x", stats, max_retries=2)


def test_edinet_http_client_reconnects_after_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    stats = make_stats()
    fake1 = _FakeHTTPSConnection([OSError("connection reset")])
    fake2 = _FakeHTTPSConnection([(200, b"ok")])
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    # fetch_documents.pyもこのテストファイルも同じhttp.clientモジュールをimportしているため、
    # モジュールオブジェクト自体を差し替える（fetch_documents.http.client経由のアクセスは
    # mypy strictが「暗黙の再エクスポート」として拒否するため）。
    monkeypatch.setattr(http.client, "HTTPSConnection", _connection_factory(fake1, fake2))

    client = fetch_documents.EdinetHttpClient()
    body = client.get("/x", stats)

    assert body == b"ok"
    assert fake1.closed  # 壊れた接続はcloseされ、新しい接続に張り替えられる
    assert client._conn is fake2


# --- fetch_day -----------------------------------------------------------------


def test_fetch_day_raises_on_error_status(monkeypatch: pytest.MonkeyPatch) -> None:
    stats = make_stats()
    client = make_client((200, make_response("400", [])))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with pytest.raises(RuntimeError, match="400"):
        fetch_documents.fetch_day(client, "2026-08-13", "dummy-key", stats)


def test_fetch_day_retries_on_embedded_429_status(monkeypatch: pytest.MonkeyPatch) -> None:
    stats = make_stats()
    client = make_client(
        (200, make_response("429", [])),
        (200, make_response("200", [{"docID": "S100AAAA"}])),
    )
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    data = fetch_documents.fetch_day(client, "2026-08-13", "dummy-key", stats)
    assert data["results"][0]["docID"] == "S100AAAA"
    assert stats.rate_limit_retries == 1


# --- doc_output_path / save_atomic ------------------------------------------------


def test_date_hierarchy_dir_splits_into_yyyy_mm_dd(tmp_path: Path) -> None:
    # 1年365個・10年3650個のディレクトリがdata_dir直下にフラットに並ぶのを避けるため
    assert fetch_documents.date_hierarchy_dir(tmp_path, "2026-08-13") == tmp_path / "2026" / "08" / "13"


def test_doc_output_path_naming(tmp_path: Path) -> None:
    # 日付はyyyy/mm/ddの3階層、typeはその下、docIDはさらにその下
    # （同一企業・同一日の複数docID間でのファイル衝突を防ぐため）
    base = tmp_path / "2026" / "08" / "13" / "E04693"
    path_xbrl = fetch_documents.doc_output_path(tmp_path, "2026-08-13", "E04693", "S100YVG3", 1)
    assert path_xbrl == base / "xbrl" / "S100YVG3"  # ディレクトリ

    path_csv = fetch_documents.doc_output_path(tmp_path, "2026-08-13", "E04693", "S100YVG3", 5)
    assert path_csv == base / "csv" / "S100YVG3"  # ディレクトリ

    path_pdf = fetch_documents.doc_output_path(tmp_path, "2026-08-13", "E04693", "S100YVG3", 2)
    assert path_pdf == base / "pdf" / "S100YVG3.pdf"  # 単一ファイル


def test_save_atomic_writes_file(tmp_path: Path) -> None:
    dest = tmp_path / "2026-08-13" / "E04693" / "S100YVG3_pdf.pdf"
    fetch_documents.save_atomic(dest, b"pdf-bytes")
    assert dest.read_bytes() == b"pdf-bytes"
    assert not (dest.parent / (dest.name + ".tmp")).exists()


def test_save_atomic_leaves_no_partial_file_on_write_failure(tmp_path: Path) -> None:
    dest = tmp_path / "2026-08-13" / "E04693" / "S100YVG3_pdf.pdf"
    dest.parent.mkdir(parents=True)

    with patch("pathlib.Path.write_bytes", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            fetch_documents.save_atomic(dest, b"pdf-bytes")

    assert not dest.exists()
    assert not (dest.parent / (dest.name + ".tmp")).exists()


# --- list_response_path / save_list_response ---------------------------------------


def test_list_response_path_naming(tmp_path: Path) -> None:
    # 書類本体（{yyyy}/{mm}/{dd}/{edinetCode}/...）とは別の response/ 配下にまとめ、
    # 日付をyyyy/mm/ddの3階層に分ける（ファイル名にはfileDateを含めない）
    path = fetch_documents.list_response_path(tmp_path, "2026-08-13")
    assert path == tmp_path / "response" / "2026" / "08" / "13" / "document_list.json"


def test_save_list_response_writes_raw_json(tmp_path: Path) -> None:
    data = {
        "metadata": {"status": "200"},
        "results": [{"docID": "S100AAAA", "edinetCode": "E00001", "secCode": "12340"}],
    }
    fetch_documents.save_list_response(tmp_path, "2026-08-13", data)

    path = tmp_path / "response" / "2026" / "08" / "13" / "document_list.json"
    assert json.loads(path.read_text(encoding="utf-8")) == data


def test_save_list_response_overwrites_on_rerun(tmp_path: Path) -> None:
    fetch_documents.save_list_response(tmp_path, "2026-08-13", {"results": [{"docID": "OLD"}]})
    fetch_documents.save_list_response(tmp_path, "2026-08-13", {"results": [{"docID": "NEW"}]})

    path = tmp_path / "response" / "2026" / "08" / "13" / "document_list.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {"results": [{"docID": "NEW"}]}


# --- extract_and_gzip --------------------------------------------------------------


def make_zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_extract_and_gzip_writes_each_file_individually_compressed(tmp_path: Path) -> None:
    zip_bytes = make_zip_bytes({
        "XBRL_TO_CSV/honbun.csv": b"a,b,c\n1,2,3\n",
        "XBRL_TO_CSV/audit.csv": b"x,y\n9,9\n",
    })
    dest_dir = tmp_path / "S100AAAA_csv"

    file_count, total_bytes = fetch_documents.extract_and_gzip(zip_bytes, dest_dir)

    assert file_count == 2
    honbun_gz = dest_dir / "XBRL_TO_CSV" / "honbun.csv.gz"
    audit_gz = dest_dir / "XBRL_TO_CSV" / "audit.csv.gz"
    assert honbun_gz.exists() and audit_gz.exists()
    assert gzip.decompress(honbun_gz.read_bytes()) == b"a,b,c\n1,2,3\n"
    assert gzip.decompress(audit_gz.read_bytes()) == b"x,y\n9,9\n"
    assert total_bytes == honbun_gz.stat().st_size + audit_gz.stat().st_size
    assert not (dest_dir.parent / (dest_dir.name + ".tmp")).exists()


def test_extract_and_gzip_strips_prefix_when_specified(tmp_path: Path) -> None:
    zip_bytes = make_zip_bytes({"XBRL_TO_CSV/honbun.csv": b"a,b,c\n1,2,3\n"})
    dest_dir = tmp_path / "S100AAAA_csv"

    file_count, _ = fetch_documents.extract_and_gzip(zip_bytes, dest_dir, strip_prefix="XBRL_TO_CSV/")

    assert file_count == 1
    flat_gz = dest_dir / "honbun.csv.gz"  # XBRL_TO_CSV/ が除去されフラットになっている
    assert flat_gz.exists()
    assert not (dest_dir / "XBRL_TO_CSV").exists()
    assert gzip.decompress(flat_gz.read_bytes()) == b"a,b,c\n1,2,3\n"


def test_extract_and_gzip_leaves_no_partial_dir_on_failure(tmp_path: Path) -> None:
    zip_bytes = make_zip_bytes({"a.csv": b"1,2,3\n"})
    dest_dir = tmp_path / "S100AAAA_csv"

    with patch("gzip.open", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            fetch_documents.extract_and_gzip(zip_bytes, dest_dir)

    assert not dest_dir.exists()
    assert not (dest_dir.parent / (dest_dir.name + ".tmp")).exists()


# --- compute_enabled_types --------------------------------------------------------


def test_compute_enabled_types_defaults_to_all_when_none_specified() -> None:
    assert fetch_documents.compute_enabled_types(False, False, False) == {1, 2, 5}


def test_compute_enabled_types_restricts_to_specified_flags() -> None:
    assert fetch_documents.compute_enabled_types(xbrl=False, pdf=True, csv=True) == {2, 5}
    assert fetch_documents.compute_enabled_types(xbrl=True, pdf=False, csv=False) == {1}


# --- download_doc_files ----------------------------------------------------------


def test_download_doc_files_skips_existing_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stats = make_stats()
    doc = {"docID": "S100AAAA", "edinetCode": "E00001", "xbrlFlag": "1", "pdfFlag": "0", "csvFlag": "0"}
    dest = fetch_documents.doc_output_path(tmp_path, "2026-08-13", "E00001", "S100AAAA", 1)
    dest.mkdir(parents=True)
    (dest / "already-here.gz").write_bytes(b"x")

    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with patch("fetch_documents.fetch_document_file") as mock_fetch:
        ok = fetch_documents.download_doc_files(
            fetch_documents.EdinetHttpClient(), doc, "2026-08-13", tmp_path, "key", 0, stats, TEST_LOGGER, ALL_TYPES
        )

    assert ok is True
    mock_fetch.assert_not_called()


def test_download_doc_files_skips_types_not_in_enabled_types(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stats = make_stats()
    # 3つとも取得可能だが、csv・pdfのみ有効化（xbrlは除外）
    doc = {"docID": "S100AAAA", "edinetCode": "E00001", "xbrlFlag": "1", "pdfFlag": "1", "csvFlag": "1"}
    csv_zip = make_zip_bytes({"XBRL_TO_CSV/honbun.csv": b"a,b,c\n"})

    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with patch("fetch_documents.fetch_document_file", side_effect=[b"pdf-data", csv_zip]) as mock_fetch:
        ok = fetch_documents.download_doc_files(
            fetch_documents.EdinetHttpClient(), doc, "2026-08-13", tmp_path, "key", 0, stats, TEST_LOGGER, {2, 5}
        )

    assert ok is True
    assert mock_fetch.call_count == 2  # xbrlは呼ばれない（pdf・csvのみ）
    called_types = [call.args[2] for call in mock_fetch.call_args_list]  # (client, doc_id, type_code, ...)
    assert 1 not in called_types
    assert not (tmp_path / "2026" / "08" / "13" / "E00001" / "xbrl").exists()


def test_download_doc_files_downloads_missing_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stats = make_stats()
    doc = {"docID": "S100AAAA", "edinetCode": "E00001", "xbrlFlag": "1", "pdfFlag": "1", "csvFlag": "0"}
    xbrl_zip = make_zip_bytes({"XBRL/PublicDoc/a.xbrl": b"xbrl-data"})

    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with patch("fetch_documents.fetch_document_file", side_effect=[xbrl_zip, b"pdf-data"]) as mock_fetch:
        ok = fetch_documents.download_doc_files(
            fetch_documents.EdinetHttpClient(), doc, "2026-08-13", tmp_path, "key", 0, stats, TEST_LOGGER, ALL_TYPES
        )

    assert ok is True
    assert mock_fetch.call_count == 2  # xbrl + pdf
    assert stats.downloaded_count == 2  # 展開後1ファイル(xbrl) + pdf1ファイル
    xbrl_gz = tmp_path / "2026" / "08" / "13" / "E00001" / "xbrl" / "S100AAAA" / "XBRL" / "PublicDoc" / "a.xbrl.gz"
    assert gzip.decompress(xbrl_gz.read_bytes()) == b"xbrl-data"
    assert (tmp_path / "2026" / "08" / "13" / "E00001" / "pdf" / "S100AAAA.pdf").read_bytes() == b"pdf-data"


def test_download_doc_files_flattens_csv_xbrl_to_csv_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stats = make_stats()
    doc = {"docID": "S100AAAA", "edinetCode": "E00001", "xbrlFlag": "0", "pdfFlag": "0", "csvFlag": "1"}
    csv_zip = make_zip_bytes({"XBRL_TO_CSV/honbun.csv": b"a,b,c\n"})

    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with patch("fetch_documents.fetch_document_file", return_value=csv_zip):
        ok = fetch_documents.download_doc_files(
            fetch_documents.EdinetHttpClient(), doc, "2026-08-13", tmp_path, "key", 0, stats, TEST_LOGGER, ALL_TYPES
        )

    assert ok is True
    csv_gz = tmp_path / "2026" / "08" / "13" / "E00001" / "csv" / "S100AAAA" / "honbun.csv.gz"  # XBRL_TO_CSV/無し
    assert csv_gz.exists()
    assert not (tmp_path / "2026" / "08" / "13" / "E00001" / "csv" / "S100AAAA" / "XBRL_TO_CSV").exists()


def test_download_doc_files_returns_false_on_partial_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stats = make_stats()
    doc = {"docID": "S100AAAA", "edinetCode": "E00001", "xbrlFlag": "1", "pdfFlag": "1", "csvFlag": "0"}
    xbrl_zip = make_zip_bytes({"XBRL/PublicDoc/a.xbrl": b"xbrl-data"})

    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with patch(
        "fetch_documents.fetch_document_file", side_effect=[xbrl_zip, RuntimeError("boom")]
    ):
        ok = fetch_documents.download_doc_files(
            fetch_documents.EdinetHttpClient(), doc, "2026-08-13", tmp_path, "key", 0, stats, TEST_LOGGER, ALL_TYPES
        )

    assert ok is False
    assert stats.downloaded_count == 1  # xbrl展開分だけ成功


# --- run / process_day -----------------------------------------------------------


def test_run_skips_already_done_dates_unless_forced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = fetch_documents.init_db(tmp_path / "index.db")
    fetch_documents.store_progress(conn, "2026-08-13", "done", 0, None)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    client = fetch_documents.EdinetHttpClient()
    with patch("fetch_documents.fetch_day") as mock_fetch_day:
        mock_fetch_day.return_value = {"results": []}
        fetch_documents.run(
            conn, client, "dummy-key", datetime.date(2026, 8, 13), datetime.date(2026, 8, 13),
            delay=0, force=False, data_dir=tmp_path, logger=TEST_LOGGER, log_path="test.log",
            enabled_types=ALL_TYPES,
        )
        mock_fetch_day.assert_not_called()

        fetch_documents.run(
            conn, client, "dummy-key", datetime.date(2026, 8, 13), datetime.date(2026, 8, 13),
            delay=0, force=True, data_dir=tmp_path, logger=TEST_LOGGER, log_path="test.log",
            enabled_types=ALL_TYPES,
        )
        mock_fetch_day.assert_called_once()


def test_process_day_marks_done_when_all_downloads_succeed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = fetch_documents.init_db(tmp_path / "index.db")
    stats = make_stats()
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    data = {"results": [{"docID": "S100AAAA", "edinetCode": "E00001", "secCode": "12340"}]}
    with patch("fetch_documents.fetch_day", return_value=data), \
         patch("fetch_documents.download_doc_files", return_value=True):
        count = fetch_documents.process_day(
            conn, fetch_documents.EdinetHttpClient(), "2026-08-13", "key", tmp_path, 0, stats,
            TEST_LOGGER, "test.log", ALL_TYPES
        )

    assert count == 1
    assert fetch_documents.already_done(conn, "2026-08-13")
    assert stats.days_processed == ["2026-08-13"]
    assert stats.days_failed == {}

    # 一覧APIの生レスポンスも保存される（後段がEDINET APIへ再アクセスせずに済むように）
    saved = json.loads(
        (tmp_path / "response" / "2026" / "08" / "13" / "document_list.json").read_text(encoding="utf-8")
    )
    assert saved == data


def test_process_day_logs_progress_every_n_docs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = fetch_documents.init_db(tmp_path / "index.db")
    stats = make_stats()
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    n_docs = fetch_documents.PROGRESS_LOG_INTERVAL_DOCS + 5  # 間隔を1回だけ跨ぐ件数
    data = {
        "results": [
            {"docID": f"S1{i:06d}", "edinetCode": "E00001", "secCode": "12340"}
            for i in range(n_docs)
        ]
    }
    with patch("fetch_documents.fetch_day", return_value=data), \
         patch("fetch_documents.download_doc_files", return_value=True), \
         patch.object(TEST_LOGGER, "info") as mock_info:
        fetch_documents.process_day(
            conn, fetch_documents.EdinetHttpClient(), "2026-08-13", "key", tmp_path, 0, stats,
            TEST_LOGGER, "test.log", ALL_TYPES
        )

    progress_calls = [
        call for call in mock_info.call_args_list if "進捗" in call.args[0]
    ]
    assert len(progress_calls) == 1
    assert f"{fetch_documents.PROGRESS_LOG_INTERVAL_DOCS}/{n_docs}件" in progress_calls[0].args[0]


def test_process_day_marks_error_when_a_download_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = fetch_documents.init_db(tmp_path / "index.db")
    stats = make_stats()
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    data = {"results": [{"docID": "S100AAAA", "edinetCode": "E00001", "secCode": "12340"}]}
    with patch("fetch_documents.fetch_day", return_value=data), \
         patch("fetch_documents.download_doc_files", return_value=False):
        fetch_documents.process_day(
            conn, fetch_documents.EdinetHttpClient(), "2026-08-13", "key", tmp_path, 0, stats,
            TEST_LOGGER, "test.log", ALL_TYPES
        )

    assert not fetch_documents.already_done(conn, "2026-08-13")
    assert "2026-08-13" in stats.days_failed


def test_process_day_filters_out_docs_without_seccode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = fetch_documents.init_db(tmp_path / "index.db")
    stats = make_stats()
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    data = {
        "results": [
            {"docID": "S100AAAA", "edinetCode": "E00001", "secCode": "12340"},
            {"docID": "S100BBBB", "edinetCode": "E00002", "secCode": None},
        ]
    }
    with patch("fetch_documents.fetch_day", return_value=data), \
         patch("fetch_documents.download_doc_files", return_value=True) as mock_dl:
        count = fetch_documents.process_day(
            conn, fetch_documents.EdinetHttpClient(), "2026-08-13", "key", tmp_path, 0, stats,
            TEST_LOGGER, "test.log", ALL_TYPES
        )

    assert count == 1
    assert mock_dl.call_count == 1


# --- Slack通知 ---------------------------------------------------------------


def test_format_bytes() -> None:
    assert fetch_documents.format_bytes(500 * 1024) == "0.5MB"
    assert fetch_documents.format_bytes(2 * 1024 * 1024 * 1024) == "2.0GB"


def test_build_slack_message_success() -> None:
    stats = fetch_documents.RunStats(
        start=datetime.date(2026, 8, 24), end=datetime.date(2026, 8, 26),
        days_processed=["2026-08-24", "2026-08-25", "2026-08-26"],
        downloaded_count=711, downloaded_bytes=128_400_000, rate_limit_retries=2,
    )
    message = fetch_documents.build_slack_message(stats, free_bytes=421_300_000_000)
    assert message.startswith("✅")
    assert "3日処理" in message
    assert "711件" in message
    assert "429発生: 2回" in message


def test_build_slack_message_failure_includes_error_detail() -> None:
    stats = fetch_documents.RunStats(
        start=datetime.date(2026, 8, 24), end=datetime.date(2026, 8, 26),
        days_processed=["2026-08-24", "2026-08-25", "2026-08-26"],
        days_failed={"2026-08-25": "EDINET APIエラー status=500 message=boom"},
    )
    message = fetch_documents.build_slack_message(stats, free_bytes=0)
    assert message.startswith("❌")
    assert "1/3日" in message
    assert "2026-08-25" in message
    assert "status=500" in message


def test_send_slack_notification_failure_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    with patch("fetch_documents.urllib.request.urlopen", side_effect=OSError("network down")):
        fetch_documents.send_slack_notification("https://hooks.slack.com/x", "hello", TEST_LOGGER)
    # 例外が上がらなければOK


def test_send_slack_notification_posts_json(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_resp = MagicMock()
    mock_resp.__enter__.return_value = mock_resp
    captured: dict[str, Any] = {}

    def fake_urlopen(req: Any, timeout: float = 10) -> MagicMock:
        captured["url"] = req.full_url
        captured["data"] = json.loads(req.data.decode("utf-8"))
        return mock_resp

    with patch("fetch_documents.urllib.request.urlopen", side_effect=fake_urlopen):
        fetch_documents.send_slack_notification("https://hooks.slack.com/x", "hello", TEST_LOGGER)

    assert captured["url"] == "https://hooks.slack.com/x"
    assert captured["data"] == {"text": "hello"}

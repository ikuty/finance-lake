from __future__ import annotations

import datetime
import gzip
import io
import json
import logging
import sys
import time
import urllib.error
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


# --- date_range / DB progress ------------------------------------------------


def test_date_range_inclusive() -> None:
    start = datetime.date(2026, 1, 1)
    end = datetime.date(2026, 1, 3)
    assert list(fetch_documents.date_range(start, end)) == [
        datetime.date(2026, 1, 1),
        datetime.date(2026, 1, 2),
        datetime.date(2026, 1, 3),
    ]


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


# --- _http_get: 共通のHTTPフェッチ+リトライ -----------------------------------


def _mock_urlopen_sequence(*responses: Any) -> Any:
    def fake_urlopen(*args: object, **kwargs: object) -> MagicMock:
        item = responses[fake_urlopen.calls]  # type: ignore[attr-defined]
        fake_urlopen.calls += 1  # type: ignore[attr-defined]
        if isinstance(item, Exception):
            raise item
        mock_resp = MagicMock()
        mock_resp.read.return_value = item
        mock_resp.__enter__.return_value = mock_resp
        return mock_resp

    fake_urlopen.calls = 0  # type: ignore[attr-defined]
    return fake_urlopen


def test_http_get_retries_on_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    stats = make_stats()
    err_429 = urllib.error.HTTPError("http://x", 429, "rate limited", None, None)  # type: ignore[arg-type]
    fake = _mock_urlopen_sequence(err_429, b"ok")
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with patch("fetch_documents.urllib.request.urlopen", side_effect=fake):
        body = fetch_documents._http_get("http://example/", stats)
    assert body == b"ok"
    assert stats.rate_limit_retries == 1


def test_http_get_retries_on_network_error_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    stats = make_stats()
    fake = _mock_urlopen_sequence(urllib.error.URLError("timed out"), b"ok")
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with patch("fetch_documents.urllib.request.urlopen", side_effect=fake):
        body = fetch_documents._http_get("http://example/", stats)
    assert body == b"ok"
    assert stats.rate_limit_retries == 0  # ネットワークエラーは429カウントに含めない


def test_http_get_retries_on_5xx_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    stats = make_stats()
    err_500 = urllib.error.HTTPError("http://x", 500, "server error", None, None)  # type: ignore[arg-type]
    fake = _mock_urlopen_sequence(err_500, b"ok")
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with patch("fetch_documents.urllib.request.urlopen", side_effect=fake):
        body = fetch_documents._http_get("http://example/", stats)
    assert body == b"ok"


def test_http_get_fails_immediately_on_404(monkeypatch: pytest.MonkeyPatch) -> None:
    stats = make_stats()
    err_404 = urllib.error.HTTPError("http://x", 404, "not found", None, None)  # type: ignore[arg-type]
    fake = _mock_urlopen_sequence(err_404)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with patch("fetch_documents.urllib.request.urlopen", side_effect=fake):
        with pytest.raises(RuntimeError, match="404"):
            fetch_documents._http_get("http://example/", stats)
    assert fake.calls == 1  # リトライしない


def test_http_get_raises_rate_limited_after_max_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    stats = make_stats()
    err_429 = urllib.error.HTTPError("http://x", 429, "rate limited", None, None)  # type: ignore[arg-type]
    fake = _mock_urlopen_sequence(*([err_429] * 3))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with patch("fetch_documents.urllib.request.urlopen", side_effect=fake):
        with pytest.raises(fetch_documents.RateLimitedError):
            fetch_documents._http_get("http://example/", stats, max_retries=2)


# --- fetch_day -----------------------------------------------------------------


def test_fetch_day_raises_on_error_status(monkeypatch: pytest.MonkeyPatch) -> None:
    stats = make_stats()
    fake = _mock_urlopen_sequence(make_response("400", []))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with patch("fetch_documents.urllib.request.urlopen", side_effect=fake):
        with pytest.raises(RuntimeError, match="400"):
            fetch_documents.fetch_day("2026-08-13", "dummy-key", stats)


def test_fetch_day_retries_on_embedded_429_status(monkeypatch: pytest.MonkeyPatch) -> None:
    stats = make_stats()
    fake = _mock_urlopen_sequence(
        make_response("429", []),
        make_response("200", [{"docID": "S100AAAA"}]),
    )
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with patch("fetch_documents.urllib.request.urlopen", side_effect=fake):
        data = fetch_documents.fetch_day("2026-08-13", "dummy-key", stats)
    assert data["results"][0]["docID"] == "S100AAAA"
    assert stats.rate_limit_retries == 1


# --- doc_output_path / save_atomic ------------------------------------------------


def test_doc_output_path_naming(tmp_path: Path) -> None:
    # typeが最上位、docIDはその下（同一企業・同一日の複数docID間でのファイル衝突を防ぐため）
    path_xbrl = fetch_documents.doc_output_path(tmp_path, "2026-08-13", "E04693", "S100YVG3", 1)
    assert path_xbrl == tmp_path / "2026-08-13" / "E04693" / "xbrl" / "S100YVG3"  # ディレクトリ

    path_csv = fetch_documents.doc_output_path(tmp_path, "2026-08-13", "E04693", "S100YVG3", 5)
    assert path_csv == tmp_path / "2026-08-13" / "E04693" / "csv" / "S100YVG3"  # ディレクトリ

    path_pdf = fetch_documents.doc_output_path(tmp_path, "2026-08-13", "E04693", "S100YVG3", 2)
    assert path_pdf == tmp_path / "2026-08-13" / "E04693" / "pdf" / "S100YVG3.pdf"  # 単一ファイル


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


# --- download_doc_files ----------------------------------------------------------


def test_download_doc_files_skips_existing_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stats = make_stats()
    doc = {"docID": "S100AAAA", "edinetCode": "E00001", "xbrlFlag": "1", "pdfFlag": "0", "csvFlag": "0"}
    dest = fetch_documents.doc_output_path(tmp_path, "2026-08-13", "E00001", "S100AAAA", 1)
    dest.mkdir(parents=True)
    (dest / "already-here.gz").write_bytes(b"x")

    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with patch("fetch_documents.fetch_document_file") as mock_fetch:
        ok = fetch_documents.download_doc_files(doc, "2026-08-13", tmp_path, "key", 0, stats, TEST_LOGGER)

    assert ok is True
    mock_fetch.assert_not_called()


def test_download_doc_files_downloads_missing_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stats = make_stats()
    doc = {"docID": "S100AAAA", "edinetCode": "E00001", "xbrlFlag": "1", "pdfFlag": "1", "csvFlag": "0"}
    xbrl_zip = make_zip_bytes({"XBRL/PublicDoc/a.xbrl": b"xbrl-data"})

    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with patch("fetch_documents.fetch_document_file", side_effect=[xbrl_zip, b"pdf-data"]) as mock_fetch:
        ok = fetch_documents.download_doc_files(doc, "2026-08-13", tmp_path, "key", 0, stats, TEST_LOGGER)

    assert ok is True
    assert mock_fetch.call_count == 2  # xbrl + pdf
    assert stats.downloaded_count == 2  # 展開後1ファイル(xbrl) + pdf1ファイル
    xbrl_gz = tmp_path / "2026-08-13" / "E00001" / "xbrl" / "S100AAAA" / "XBRL" / "PublicDoc" / "a.xbrl.gz"
    assert gzip.decompress(xbrl_gz.read_bytes()) == b"xbrl-data"
    assert (tmp_path / "2026-08-13" / "E00001" / "pdf" / "S100AAAA.pdf").read_bytes() == b"pdf-data"


def test_download_doc_files_flattens_csv_xbrl_to_csv_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stats = make_stats()
    doc = {"docID": "S100AAAA", "edinetCode": "E00001", "xbrlFlag": "0", "pdfFlag": "0", "csvFlag": "1"}
    csv_zip = make_zip_bytes({"XBRL_TO_CSV/honbun.csv": b"a,b,c\n"})

    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with patch("fetch_documents.fetch_document_file", return_value=csv_zip):
        ok = fetch_documents.download_doc_files(doc, "2026-08-13", tmp_path, "key", 0, stats, TEST_LOGGER)

    assert ok is True
    csv_gz = tmp_path / "2026-08-13" / "E00001" / "csv" / "S100AAAA" / "honbun.csv.gz"  # XBRL_TO_CSV/無し
    assert csv_gz.exists()
    assert not (tmp_path / "2026-08-13" / "E00001" / "csv" / "S100AAAA" / "XBRL_TO_CSV").exists()


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
        ok = fetch_documents.download_doc_files(doc, "2026-08-13", tmp_path, "key", 0, stats, TEST_LOGGER)

    assert ok is False
    assert stats.downloaded_count == 1  # xbrl展開分だけ成功


# --- run / process_day -----------------------------------------------------------


def test_run_skips_already_done_dates_unless_forced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = fetch_documents.init_db(tmp_path / "index.db")
    fetch_documents.store_progress(conn, "2026-08-13", "done", 0, None)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    with patch("fetch_documents.fetch_day") as mock_fetch_day:
        mock_fetch_day.return_value = {"results": []}
        fetch_documents.run(
            conn, "dummy-key", datetime.date(2026, 8, 13), datetime.date(2026, 8, 13),
            delay=0, force=False, data_dir=tmp_path, logger=TEST_LOGGER, log_path="test.log",
        )
        mock_fetch_day.assert_not_called()

        fetch_documents.run(
            conn, "dummy-key", datetime.date(2026, 8, 13), datetime.date(2026, 8, 13),
            delay=0, force=True, data_dir=tmp_path, logger=TEST_LOGGER, log_path="test.log",
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
            conn, "2026-08-13", "key", tmp_path, 0, stats, TEST_LOGGER, "test.log"
        )

    assert count == 1
    assert fetch_documents.already_done(conn, "2026-08-13")
    assert stats.days_processed == ["2026-08-13"]
    assert stats.days_failed == {}


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
        fetch_documents.process_day(conn, "2026-08-13", "key", tmp_path, 0, stats, TEST_LOGGER, "test.log")

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
            conn, "2026-08-13", "key", tmp_path, 0, stats, TEST_LOGGER, "test.log"
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
            conn, "2026-08-13", "key", tmp_path, 0, stats, TEST_LOGGER, "test.log"
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

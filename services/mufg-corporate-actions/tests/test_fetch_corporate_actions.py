from __future__ import annotations

import logging
import sys
import time
import urllib.error
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fetch_corporate_actions as fca  # noqa: E402

TEST_LOGGER = logging.getLogger("test-mufg-corporate-actions")


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


# --- パス構築 ------------------------------------------------------------------


def test_date_hierarchy_dir_splits_into_yyyy_mm_dd(tmp_path: Path) -> None:
    assert fca.date_hierarchy_dir(tmp_path, "2026-09-14") == tmp_path / "2026" / "09" / "14"


def test_page_path_uses_format_name_as_filename(tmp_path: Path) -> None:
    assert fca.page_path(tmp_path, "2026-09-14", "bunkatu") == (
        tmp_path / "2026" / "09" / "14" / "bunkatu.html"
    )


# --- init_db / already_done / store_progress ----------------------------------


def test_already_done_false_when_no_record(tmp_path: Path) -> None:
    conn = fca.init_db(tmp_path / "index.db")
    assert fca.already_done(conn, "2026-09-14", "bunkatu") is False


def test_store_progress_then_already_done_true(tmp_path: Path) -> None:
    conn = fca.init_db(tmp_path / "index.db")
    fca.store_progress(conn, "2026-09-14", "bunkatu", "done", "https://kabu.com/x", None)
    assert fca.already_done(conn, "2026-09-14", "bunkatu") is True


def test_already_done_false_when_status_is_error(tmp_path: Path) -> None:
    conn = fca.init_db(tmp_path / "index.db")
    fca.store_progress(conn, "2026-09-14", "bunkatu", "error", "https://kabu.com/x", "boom")
    assert fca.already_done(conn, "2026-09-14", "bunkatu") is False


# --- fetch_one -----------------------------------------------------------------


def test_fetch_one_downloads_and_records_done(tmp_path: Path) -> None:
    conn = fca.init_db(tmp_path / "index.db")
    html = b"<html><body>dummy</body></html>"

    with patch("fetch_corporate_actions.urllib.request.urlopen", side_effect=_mock_urlopen_sequence(html)):
        ok, n_bytes = fca.fetch_one(conn, tmp_path, "2026-09-14", "bunkatu", TEST_LOGGER, force=False)

    assert ok is True
    assert n_bytes == len(html)
    assert fca.already_done(conn, "2026-09-14", "bunkatu")
    saved = fca.page_path(tmp_path, "2026-09-14", "bunkatu")
    assert saved.read_bytes() == html
    assert not saved.with_suffix(".html.tmp").exists()


def test_fetch_one_skips_when_already_done(tmp_path: Path) -> None:
    conn = fca.init_db(tmp_path / "index.db")
    fca.store_progress(conn, "2026-09-14", "bunkatu", "done", "https://kabu.com/x", None)

    with patch("fetch_corporate_actions.urllib.request.urlopen") as mock_urlopen:
        ok, n_bytes = fca.fetch_one(conn, tmp_path, "2026-09-14", "bunkatu", TEST_LOGGER, force=False)

    mock_urlopen.assert_not_called()
    assert ok is True
    assert n_bytes == 0


def test_fetch_one_force_revisits_done(tmp_path: Path) -> None:
    conn = fca.init_db(tmp_path / "index.db")
    fca.store_progress(conn, "2026-09-14", "bunkatu", "done", "https://kabu.com/x", None)
    html = b"<html>new content</html>"

    with patch("fetch_corporate_actions.urllib.request.urlopen", side_effect=_mock_urlopen_sequence(html)) as m:
        ok, n_bytes = fca.fetch_one(conn, tmp_path, "2026-09-14", "bunkatu", TEST_LOGGER, force=True)

    m.assert_called_once()
    assert ok is True
    saved = fca.page_path(tmp_path, "2026-09-14", "bunkatu")
    assert saved.read_bytes() == html


def test_fetch_one_marks_error_on_failure(tmp_path: Path) -> None:
    conn = fca.init_db(tmp_path / "index.db")

    with patch("fetch_corporate_actions.urllib.request.urlopen", side_effect=RuntimeError("boom")):
        ok, n_bytes = fca.fetch_one(conn, tmp_path, "2026-09-14", "bunkatu", TEST_LOGGER, force=False)

    assert ok is False
    assert n_bytes == 0
    row = conn.execute(
        "SELECT status, message FROM fetch_progress WHERE period = ? AND format = ?",
        ("2026-09-14", "bunkatu"),
    ).fetchone()
    assert row is not None
    assert row[0] == "error"
    assert "boom" in row[1]


# --- build_slack_message --------------------------------------------------------


def test_build_slack_message_all_ok() -> None:
    results = {"bunkatu": True, "gensi": True, "syougou_henkou": True}
    msg = fca.build_slack_message("2026-09-14", results, 1024 * 1024)
    assert msg.startswith("✅")
    assert "株式分割: OK" in msg
    assert "1.00MB" in msg


def test_build_slack_message_with_failure() -> None:
    results = {"bunkatu": True, "gensi": False, "syougou_henkou": True}
    msg = fca.build_slack_message("2026-09-14", results, 0)
    assert msg.startswith("❌")
    assert "株式併合: NG" in msg


# --- _http_get -------------------------------------------------------------------


def test_http_get_retries_on_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    err_429 = urllib.error.HTTPError("http://x", 429, "rate limited", None, None)  # type: ignore[arg-type]
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with patch(
        "fetch_corporate_actions.urllib.request.urlopen", side_effect=_mock_urlopen_sequence(err_429, b"ok")
    ):
        assert fca._http_get("http://example/") == b"ok"


def test_http_get_raises_immediately_on_404(monkeypatch: pytest.MonkeyPatch) -> None:
    err_404 = urllib.error.HTTPError("http://x", 404, "not found", None, None)  # type: ignore[arg-type]
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with patch("fetch_corporate_actions.urllib.request.urlopen", side_effect=err_404):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            fca._http_get("http://example/")
    assert exc_info.value.code == 404

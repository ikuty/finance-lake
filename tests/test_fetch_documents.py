from __future__ import annotations

import datetime
import json
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fetch_documents  # noqa: E402


def make_response(status: str, results: list[dict[str, Any]]) -> bytes:
    return json.dumps({"metadata": {"status": status}, "results": results}).encode("utf-8")


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


def test_store_day_marks_done_with_doc_count(tmp_path: Path) -> None:
    conn = fetch_documents.init_db(tmp_path / "index.db")
    data = {
        "results": [
            {"docID": "S100AAAA", "edinetCode": "E00001", "secCode": "12340", "filerName": "テスト株式会社"},
        ]
    }
    count = fetch_documents.store_day(conn, "2026-08-13", data)
    assert count == 1
    assert fetch_documents.already_done(conn, "2026-08-13")

    row = conn.execute(
        "SELECT docCount FROM fetch_progress WHERE fileDate = ?", ("2026-08-13",)
    ).fetchone()
    assert row[0] == 1


def test_store_day_overwrites_progress_on_rerun(tmp_path: Path) -> None:
    conn = fetch_documents.init_db(tmp_path / "index.db")
    fetch_documents.store_day(conn, "2026-08-13", {"results": [{"docID": "S100AAAA"}]})
    fetch_documents.store_day(conn, "2026-08-13", {"results": [{"docID": "S100AAAA"}, {"docID": "S100BBBB"}]})

    rows = conn.execute(
        "SELECT docCount FROM fetch_progress WHERE fileDate = ?", ("2026-08-13",)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 2


def test_fetch_day_raises_on_error_status() -> None:
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = make_response("400", [])
    mock_resp.__enter__.return_value = mock_resp

    with patch("fetch_documents.urllib.request.urlopen", return_value=mock_resp):
        try:
            fetch_documents.fetch_day("2026-08-13", "dummy-key")
            assert False, "should have raised"
        except RuntimeError as e:
            assert "400" in str(e)


def test_fetch_day_retries_then_succeeds_on_429(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = [
        make_response("429", []),
        make_response("200", [{"docID": "S100AAAA"}]),
    ]

    def fake_urlopen(*args: object, **kwargs: object) -> MagicMock:
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = responses.pop(0)
        mock_resp.__enter__.return_value = mock_resp
        return mock_resp

    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with patch("fetch_documents.urllib.request.urlopen", side_effect=fake_urlopen):
        data = fetch_documents.fetch_day("2026-08-13", "dummy-key")
    assert data["results"][0]["docID"] == "S100AAAA"


def test_run_skips_already_done_dates_unless_forced(tmp_path: Path) -> None:
    conn = fetch_documents.init_db(tmp_path / "index.db")
    fetch_documents.store_day(conn, "2026-08-13", {"results": []})

    with patch("fetch_documents.fetch_day") as mock_fetch:
        mock_fetch.return_value = {"results": []}
        fetch_documents.run(
            conn,
            "dummy-key",
            datetime.date(2026, 8, 13),
            datetime.date(2026, 8, 13),
            delay=0,
            force=False,
        )
        mock_fetch.assert_not_called()

        fetch_documents.run(
            conn,
            "dummy-key",
            datetime.date(2026, 8, 13),
            datetime.date(2026, 8, 13),
            delay=0,
            force=True,
        )
        mock_fetch.assert_called_once()

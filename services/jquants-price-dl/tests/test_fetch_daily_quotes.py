from __future__ import annotations

import io
import logging
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fetch_daily_quotes as fdq  # noqa: E402

TEST_LOGGER = logging.getLogger("test-jquants-price-dl")


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def http_error(code: int, body: bytes = b"{}") -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://example/", code, "error", {}, io.BytesIO(body))  # type: ignore[arg-type]


def url_error_dns() -> urllib.error.URLError:
    return urllib.error.URLError(socket.gaierror("name resolution failed"))


# --- パス構築 ------------------------------------------------------------------


def test_date_hierarchy_dir_splits_into_yyyy_mm_dd(tmp_path: Path) -> None:
    assert fdq.date_hierarchy_dir(tmp_path, "2026-10-20") == tmp_path / "2026" / "10" / "20"


def test_quotes_path_uses_fixed_filename(tmp_path: Path) -> None:
    assert fdq.quotes_path(tmp_path, "2026-10-20") == tmp_path / "2026" / "10" / "20" / "daily_quotes.json"


# --- recent_dates --------------------------------------------------------------


def test_recent_dates_returns_window_ending_at_end_date() -> None:
    import datetime

    end = datetime.date(2026, 10, 20)
    assert fdq.recent_dates(end, 3) == ["2026-10-20", "2026-10-19", "2026-10-18"]


# --- _fetch_page / fetch_daily_quotes ------------------------------------------


def test_fetch_page_succeeds_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    body = b'{"data": [{"Code": "13010"}]}'
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=30: FakeResponse(body))

    result = fdq._fetch_page("key", "20261020", None)

    assert result == {"data": [{"Code": "13010"}]}


def test_fetch_page_dns_failure_fails_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fake_urlopen(req: Any, timeout: int = 30) -> FakeResponse:
        calls["n"] += 1
        raise url_error_dns()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError, match="名前解決"):
        fdq._fetch_page("key", "20261020", None)
    assert calls["n"] == 1  # リトライしない


def test_fetch_page_retries_on_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    responses: list[FakeResponse | Exception] = [http_error(429), FakeResponse(b'{"data": []}')]

    def fake_urlopen(req: Any, timeout: int = 30) -> FakeResponse:
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = fdq._fetch_page("key", "20261020", None)
    assert result == {"data": []}


def test_fetch_page_404_fails_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fake_urlopen(req: Any, timeout: int = 30) -> FakeResponse:
        calls["n"] += 1
        raise http_error(404, b'{"message": "not found"}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError, match="HTTPエラー"):
        fdq._fetch_page("key", "20261020", None)
    assert calls["n"] == 1  # 4xx(429以外)はリトライしない


def test_fetch_page_exhausts_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    def fake_urlopen(req: Any, timeout: int = 30) -> FakeResponse:
        raise http_error(500)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError, match="リトライ上限"):
        fdq._fetch_page("key", "20261020", None, max_retries=2)


def test_fetch_daily_quotes_follows_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = [
        b'{"data": [{"Code": "1"}], "pagination_key": "next"}',
        b'{"data": [{"Code": "2"}]}',
    ]

    def fake_urlopen(req: Any, timeout: int = 30) -> FakeResponse:
        return FakeResponse(pages.pop(0))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = fdq.fetch_daily_quotes("key", "20261020")
    assert result == {"data": [{"Code": "1"}, {"Code": "2"}]}


# --- fetch_one -----------------------------------------------------------------


def test_fetch_one_downloads_and_saves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=30: FakeResponse(b'{"data": [{}, {}]}'))

    ok, count = fdq.fetch_one("key", tmp_path, "2026-10-20", TEST_LOGGER, force=False)

    assert ok is True
    assert count == 2
    saved = fdq.quotes_path(tmp_path, "2026-10-20")
    assert saved.exists()
    assert not saved.with_suffix(".json.tmp").exists()


def test_fetch_one_skips_when_already_exists(tmp_path: Path) -> None:
    dest = fdq.quotes_path(tmp_path, "2026-10-20")
    dest.parent.mkdir(parents=True)
    dest.write_text('{"data": []}')

    ok, count = fdq.fetch_one("key", tmp_path, "2026-10-20", TEST_LOGGER, force=False)

    assert ok is True
    assert count == -1


def test_fetch_one_force_refetches_existing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dest = fdq.quotes_path(tmp_path, "2026-10-20")
    dest.parent.mkdir(parents=True)
    dest.write_text('{"data": ["old"]}')
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=30: FakeResponse(b'{"data": [{}]}'))

    ok, count = fdq.fetch_one("key", tmp_path, "2026-10-20", TEST_LOGGER, force=True)

    assert ok is True
    assert count == 1


def test_fetch_one_returns_false_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    def fake_urlopen(req: Any, timeout: int = 30) -> FakeResponse:
        raise url_error_dns()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    ok, count = fdq.fetch_one("key", tmp_path, "2026-10-20", TEST_LOGGER, force=False)

    assert ok is False
    assert count == 0
    assert not fdq.quotes_path(tmp_path, "2026-10-20").exists()


# --- build_slack_message --------------------------------------------------------


def test_build_slack_message_all_ok() -> None:
    results = {"2026-10-20": (True, 4443), "2026-10-19": (True, -1)}
    msg = fdq.build_slack_message(results)
    assert msg.startswith("✅")
    assert "2026-10-20: 4443件" in msg
    assert "2026-10-19: スキップ(既存)" in msg


def test_build_slack_message_with_failure() -> None:
    results = {"2026-10-20": (True, 4443), "2026-10-19": (False, 0)}
    msg = fdq.build_slack_message(results)
    assert msg.startswith("❌")
    assert "2026-10-19: NG" in msg

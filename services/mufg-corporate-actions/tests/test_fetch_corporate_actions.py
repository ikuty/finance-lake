from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from playwright.sync_api import Error as PlaywrightError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fetch_corporate_actions as fca  # noqa: E402

TEST_LOGGER = logging.getLogger("test-mufg-corporate-actions")


class FakePage:
    """Playwright Page の goto/content だけを持つテストダブル。

    responsesに文字列を渡すとその内容をcontent()が返す。Exceptionを渡すと
    gotoでそれをraiseする（PlaywrightErrorのリトライ挙動を検証するため）。
    """

    def __init__(self, *responses: Any) -> None:
        self._responses = list(responses)
        self._html: str | None = None
        self.goto_calls = 0

    def goto(self, url: str, timeout: int | None = None) -> None:
        self.goto_calls += 1
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        self._html = item

    def content(self) -> str:
        assert self._html is not None
        return self._html


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
    html = "<html><body>dummy</body></html>"
    page = FakePage(html)

    ok, n_bytes = fca.fetch_one(conn, tmp_path, "2026-09-14", "bunkatu", TEST_LOGGER, force=False, page=page)  # type: ignore[arg-type]

    assert ok is True
    assert n_bytes == len(html.encode("utf-8"))
    assert fca.already_done(conn, "2026-09-14", "bunkatu")
    saved = fca.page_path(tmp_path, "2026-09-14", "bunkatu")
    assert saved.read_text(encoding="utf-8") == html
    assert not saved.with_suffix(".html.tmp").exists()


def test_fetch_one_skips_when_already_done(tmp_path: Path) -> None:
    conn = fca.init_db(tmp_path / "index.db")
    fca.store_progress(conn, "2026-09-14", "bunkatu", "done", "https://kabu.com/x", None)
    page = FakePage()

    ok, n_bytes = fca.fetch_one(conn, tmp_path, "2026-09-14", "bunkatu", TEST_LOGGER, force=False, page=page)  # type: ignore[arg-type]

    assert page.goto_calls == 0
    assert ok is True
    assert n_bytes == 0


def test_fetch_one_force_revisits_done(tmp_path: Path) -> None:
    conn = fca.init_db(tmp_path / "index.db")
    fca.store_progress(conn, "2026-09-14", "bunkatu", "done", "https://kabu.com/x", None)
    html = "<html>new content</html>"
    page = FakePage(html)

    ok, n_bytes = fca.fetch_one(conn, tmp_path, "2026-09-14", "bunkatu", TEST_LOGGER, force=True, page=page)  # type: ignore[arg-type]

    assert page.goto_calls == 1
    assert ok is True
    saved = fca.page_path(tmp_path, "2026-09-14", "bunkatu")
    assert saved.read_text(encoding="utf-8") == html


def test_fetch_one_marks_error_on_failure(tmp_path: Path) -> None:
    conn = fca.init_db(tmp_path / "index.db")
    page = FakePage(*[PlaywrightError("boom")] * 6)  # 初回+リトライ5回、すべて失敗

    with patch("fetch_corporate_actions.time.sleep"):
        ok, n_bytes = fca.fetch_one(conn, tmp_path, "2026-09-14", "bunkatu", TEST_LOGGER, force=False, page=page)  # type: ignore[arg-type]

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


# --- _render_html -------------------------------------------------------------------


def test_render_html_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    page = FakePage(PlaywrightError("transient"), "<html>ok</html>")

    assert fca._render_html(page, "http://example/") == "<html>ok</html>"  # type: ignore[arg-type]
    assert page.goto_calls == 2


def test_render_html_raises_after_exhausting_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    page = FakePage(*[PlaywrightError("boom")] * 6)  # 初回+リトライ5回、すべて失敗

    with pytest.raises(RuntimeError, match="リトライ上限"):
        fca._render_html(page, "http://example/", max_retries=5)  # type: ignore[arg-type]
    assert page.goto_calls == 6

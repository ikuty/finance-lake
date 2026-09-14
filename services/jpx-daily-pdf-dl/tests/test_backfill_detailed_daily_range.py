from __future__ import annotations

import datetime
import sys
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import backfill_detailed_daily_range as bddr  # noqa: E402
import fetch_jpx_daily as fjd  # noqa: E402

TEST_LOGGER = bddr.setup_logger()

ARCHIVE_PAGE_1 = '<a href="/markets/statistics-equities/daily/data/stq_20260603.pdf">2026/06/03</a>'
ARCHIVE_PAGE_2 = '<a href="/markets/statistics-equities/daily/data/stq_20260501.pdf">2026/05/01</a>'


# --- fetch_archive_links ---------------------------------------------------------------


def test_fetch_archive_links_stops_at_first_empty_page() -> None:
    pages = {1: ARCHIVE_PAGE_1, 2: ARCHIVE_PAGE_2, 3: ""}

    def fake_http_get(url: str, **kwargs: object) -> bytes:
        for page, html in pages.items():
            if f"00-archives-{page:02d}.html" in url:
                return html.encode("utf-8")
        raise AssertionError(f"unexpected url: {url}")

    with patch("backfill_detailed_daily_range._http_get", side_effect=fake_http_get) as mock_get:
        links = bddr.fetch_archive_links(TEST_LOGGER, max_pages=5)

    assert links == {
        "2026-06-03": "/markets/statistics-equities/daily/data/stq_20260603.pdf",
        "2026-05-01": "/markets/statistics-equities/daily/data/stq_20260501.pdf",
    }
    # page 3が空だったので、page 4・5へは進まない
    assert mock_get.call_count == 3


def test_fetch_archive_links_stops_at_404() -> None:
    err_404 = urllib.error.HTTPError("http://x", 404, "not found", None, None)  # type: ignore[arg-type]
    pages = {1: ARCHIVE_PAGE_1.encode("utf-8")}

    def fake_http_get(url: str, **kwargs: object) -> bytes:
        if "00-archives-01.html" in url:
            return pages[1]
        raise err_404

    with patch("backfill_detailed_daily_range._http_get", side_effect=fake_http_get) as mock_get:
        links = bddr.fetch_archive_links(TEST_LOGGER, max_pages=5)

    assert links == {"2026-06-03": "/markets/statistics-equities/daily/data/stq_20260603.pdf"}
    assert mock_get.call_count == 2


# --- backfill_range ---------------------------------------------------------------


def test_backfill_range_downloads_dates_present_in_links(tmp_path: Path) -> None:
    conn = fjd.init_db(tmp_path / "index.db")
    links = {"2026-06-03": "/markets/statistics-equities/daily/data/stq_20260603.pdf"}

    with patch("backfill_detailed_daily_range._http_get", return_value=b"%PDF-content"):
        stats = bddr.backfill_range(
            conn, tmp_path, datetime.date(2026, 6, 1), datetime.date(2026, 6, 5), TEST_LOGGER, links
        )

    assert stats.processed == [("2026-06-03", fjd.FORMAT_DETAILED_DAILY)]
    assert fjd.already_done(conn, "2026-06-03", fjd.FORMAT_DETAILED_DAILY)
    assert fjd.detailed_daily_path(tmp_path, "2026-06-03").read_bytes() == b"%PDF-content"


def test_backfill_range_skips_dates_not_in_links(tmp_path: Path) -> None:
    # 週末・休日、またはローリングウィンドウの範囲外(取得不可)の日は静かに無視する
    conn = fjd.init_db(tmp_path / "index.db")

    with patch("backfill_detailed_daily_range._http_get") as mock_get:
        stats = bddr.backfill_range(
            conn, tmp_path, datetime.date(2026, 6, 1), datetime.date(2026, 6, 5), TEST_LOGGER, links={}
        )

    mock_get.assert_not_called()
    assert stats.processed == []
    assert conn.execute("SELECT COUNT(*) FROM fetch_progress").fetchone()[0] == 0


def test_backfill_range_skips_already_done_dates(tmp_path: Path) -> None:
    conn = fjd.init_db(tmp_path / "index.db")
    fjd.store_progress(conn, "2026-06-03", fjd.FORMAT_DETAILED_DAILY, "done", "https://old", None)
    links = {"2026-06-03": "/markets/statistics-equities/daily/data/stq_20260603.pdf"}

    with patch("backfill_detailed_daily_range._http_get") as mock_get:
        bddr.backfill_range(conn, tmp_path, datetime.date(2026, 6, 3), datetime.date(2026, 6, 3), TEST_LOGGER, links)

    mock_get.assert_not_called()


def test_backfill_range_force_revisits_done_dates(tmp_path: Path) -> None:
    conn = fjd.init_db(tmp_path / "index.db")
    fjd.store_progress(conn, "2026-06-03", fjd.FORMAT_DETAILED_DAILY, "done", "https://old", None)
    links = {"2026-06-03": "/markets/statistics-equities/daily/data/stq_20260603.pdf"}

    with patch("backfill_detailed_daily_range._http_get", return_value=b"%PDF-new") as mock_get:
        bddr.backfill_range(
            conn, tmp_path, datetime.date(2026, 6, 3), datetime.date(2026, 6, 3), TEST_LOGGER, links, force=True
        )

    mock_get.assert_called_once()
    assert fjd.detailed_daily_path(tmp_path, "2026-06-03").read_bytes() == b"%PDF-new"


def test_backfill_range_marks_error_on_failure(tmp_path: Path) -> None:
    conn = fjd.init_db(tmp_path / "index.db")
    links = {"2026-06-03": "/markets/statistics-equities/daily/data/stq_20260603.pdf"}

    with patch("backfill_detailed_daily_range._http_get", side_effect=RuntimeError("boom")):
        stats = bddr.backfill_range(
            conn, tmp_path, datetime.date(2026, 6, 3), datetime.date(2026, 6, 3), TEST_LOGGER, links
        )

    assert stats.failed == {("2026-06-03", fjd.FORMAT_DETAILED_DAILY): "boom"}
    row = conn.execute(
        "SELECT status, message FROM fetch_progress WHERE period = ? AND format = ?",
        ("2026-06-03", fjd.FORMAT_DETAILED_DAILY),
    ).fetchone()
    assert row == ("error", "boom")


def test_backfill_range_skips_existing_file_without_download(tmp_path: Path) -> None:
    # DBには記録が無いがファイルだけ既に存在するケース(自己修復)
    conn = fjd.init_db(tmp_path / "index.db")
    dest = fjd.detailed_daily_path(tmp_path, "2026-06-03")
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"existing")
    links = {"2026-06-03": "/markets/statistics-equities/daily/data/stq_20260603.pdf"}

    with patch("backfill_detailed_daily_range._http_get") as mock_get:
        bddr.backfill_range(conn, tmp_path, datetime.date(2026, 6, 3), datetime.date(2026, 6, 3), TEST_LOGGER, links)

    mock_get.assert_not_called()
    assert fjd.already_done(conn, "2026-06-03", fjd.FORMAT_DETAILED_DAILY)
    assert dest.read_bytes() == b"existing"

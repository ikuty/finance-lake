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


def _month_json(entries: dict[str, str]) -> bytes:
    import json

    table = [{"TradeDate": d.replace("-", ""), "Stocks": p} for d, p in entries.items()]
    return json.dumps({"TableDatas": table}).encode("utf-8")


# --- fetch_archive_links ---------------------------------------------------------------


def test_fetch_archive_links_merges_all_months_in_range() -> None:
    # 2026-08以前(OLD_SYSTEM_BOUNDARY_YM)はHTMLフラグメント形式になるため、
    # JSON形式のfetch_month_links経路を素直にテストできる2026-09以降の2ヶ月を使う。
    oct_json = _month_json({"2026-10-02": "/data/stq_20261002.pdf"})
    sep_json = _month_json({"2026-09-30": "/data/stq_20260930.pdf"})

    def fake_http_get(url: str, **kwargs: object) -> bytes:
        if "202610" in url:
            return oct_json
        if "202609" in url:
            return sep_json
        raise AssertionError(f"unexpected url: {url}")

    with patch("fetch_jpx_daily._http_get", side_effect=fake_http_get) as mock_get:
        links = bddr.fetch_archive_links(datetime.date(2026, 9, 30), datetime.date(2026, 10, 2), TEST_LOGGER)

    assert links == {
        "2026-10-02": "/data/stq_20261002.pdf",
        "2026-09-30": "/data/stq_20260930.pdf",
    }
    assert mock_get.call_count == 2


def test_fetch_archive_links_tolerates_months_with_no_data() -> None:
    err_404 = urllib.error.HTTPError("http://x", 404, "not found", None, None)  # type: ignore[arg-type]
    oct_json = _month_json({"2026-10-02": "/data/stq_20261002.pdf"})

    def fake_http_get(url: str, **kwargs: object) -> bytes:
        if "202610" in url:
            return oct_json
        raise err_404

    with patch("fetch_jpx_daily._http_get", side_effect=fake_http_get) as mock_get:
        links = bddr.fetch_archive_links(datetime.date(2026, 9, 30), datetime.date(2026, 10, 2), TEST_LOGGER)

    assert links == {"2026-10-02": "/data/stq_20261002.pdf"}
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

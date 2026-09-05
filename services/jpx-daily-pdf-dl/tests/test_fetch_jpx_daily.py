from __future__ import annotations

import datetime
import logging
import sys
import time
import urllib.error
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fetch_jpx_daily as fjd  # noqa: E402

TEST_LOGGER = logging.getLogger("test-jpx-daily-pdf-dl")


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


# --- today_jst / last_complete_day_jst ---------------------------------------


def test_last_complete_day_jst_is_today_jst_minus_one(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_utc = datetime.datetime(2026, 9, 3, 19, 1, 0, tzinfo=datetime.timezone.utc)

    class FixedDatetime(datetime.datetime):
        @classmethod
        def now(cls, tz: datetime.tzinfo | None = None) -> datetime.datetime:  # type: ignore[override]
            return fixed_utc.astimezone(tz) if tz else fixed_utc

    monkeypatch.setattr(datetime, "datetime", FixedDatetime)

    # 2026-09-03 19:01 UTC = 2026-09-04 04:01 JST → 前日は2026-09-03
    assert fjd.today_jst() == datetime.date(2026, 9, 4)
    assert fjd.last_complete_day_jst() == datetime.date(2026, 9, 3)


def test_date_range_inclusive() -> None:
    start = datetime.date(2026, 1, 1)
    end = datetime.date(2026, 1, 3)
    assert list(fjd.date_range(start, end)) == [
        datetime.date(2026, 1, 1),
        datetime.date(2026, 1, 2),
        datetime.date(2026, 1, 3),
    ]


# --- パス構築 ------------------------------------------------------------------


def test_date_hierarchy_dir_splits_into_yyyy_mm_dd(tmp_path: Path) -> None:
    assert fjd.date_hierarchy_dir(tmp_path, "2026-09-01") == tmp_path / "2026" / "09" / "01"


def test_detailed_daily_path(tmp_path: Path) -> None:
    path = fjd.detailed_daily_path(tmp_path, "2026-09-01")
    assert path == tmp_path / "detailed-daily" / "2026" / "09" / "01" / "stq.pdf"


def test_monthly_ohlc_path(tmp_path: Path) -> None:
    path = fjd.monthly_ohlc_path(tmp_path, "2026-09")
    assert path == tmp_path / "monthly-ohlc" / "2026" / "09" / "stq_monthly.pdf"


# --- save_atomic ---------------------------------------------------------------


def test_save_atomic_writes_file(tmp_path: Path) -> None:
    dest = tmp_path / "2026" / "09" / "01" / "stq.pdf"
    fjd.save_atomic(dest, b"pdf-bytes")
    assert dest.read_bytes() == b"pdf-bytes"
    assert not (dest.parent / (dest.name + ".tmp")).exists()


# --- HTMLパース ------------------------------------------------------------------


def test_parse_daily_links_extracts_date_to_path() -> None:
    html = """
    <a href="/markets/statistics-equities/daily/t13vrt000001ved9-att/stq_20260903.pdf">9/3</a>
    <a href="/markets/statistics-equities/daily/t13vrt000001v7aw-att/stq_20260901.pdf">9/1</a>
    """
    links = fjd.parse_daily_links(html)
    assert links == {
        "2026-09-03": "/markets/statistics-equities/daily/t13vrt000001ved9-att/stq_20260903.pdf",
        "2026-09-01": "/markets/statistics-equities/daily/t13vrt000001v7aw-att/stq_20260901.pdf",
    }


def test_parse_daily_links_returns_empty_for_no_matches() -> None:
    assert fjd.parse_daily_links("<html><body>no links here</body></html>") == {}


def test_parse_monthly_links_extracts_year_month_to_path() -> None:
    html = """
    <a href="/markets/statistics-equities/daily/tvdivq0000001jan-att/202501.pdf">Jan</a>
    <a href="/markets/statistics-equities/daily/tvdivq0000001jan-att/202508.pdf">Aug</a>
    """
    links = fjd.parse_monthly_links(html)
    assert links == {
        "2025-01": "/markets/statistics-equities/daily/tvdivq0000001jan-att/202501.pdf",
        "2025-08": "/markets/statistics-equities/daily/tvdivq0000001jan-att/202508.pdf",
    }


# --- DB進捗管理 ------------------------------------------------------------------


def test_init_db_creates_tables(tmp_path: Path) -> None:
    conn = fjd.init_db(tmp_path / "index.db")
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"fetch_progress"} <= tables


def test_store_progress_and_already_done_are_scoped_by_format(tmp_path: Path) -> None:
    conn = fjd.init_db(tmp_path / "index.db")
    fjd.store_progress(conn, "2026-09-01", "detailed-daily", "done", "https://x/1.pdf", None)

    assert fjd.already_done(conn, "2026-09-01", "detailed-daily")
    # 同じperiodでも別formatならdone扱いにならない（複合主キーの確認）
    assert not fjd.already_done(conn, "2026-09-01", "monthly-ohlc")


def test_store_progress_overwrites_same_period_and_format(tmp_path: Path) -> None:
    conn = fjd.init_db(tmp_path / "index.db")
    fjd.store_progress(conn, "2026-09", "monthly-ohlc", "error", None, "boom")
    fjd.store_progress(conn, "2026-09", "monthly-ohlc", "done", "https://x/202609.pdf", None)

    row = conn.execute(
        "SELECT status, sourceUrl, message FROM fetch_progress WHERE period = ? AND format = ?",
        ("2026-09", "monthly-ohlc"),
    ).fetchone()
    assert row == ("done", "https://x/202609.pdf", None)


# --- _http_get -------------------------------------------------------------------


def test_http_get_retries_on_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    err_429 = urllib.error.HTTPError("http://x", 429, "rate limited", None, None)  # type: ignore[arg-type]
    fake = _mock_urlopen_sequence(err_429, b"ok")
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with patch("fetch_jpx_daily.urllib.request.urlopen", side_effect=fake):
        assert fjd._http_get("http://example/") == b"ok"


def test_http_get_fails_immediately_on_404(monkeypatch: pytest.MonkeyPatch) -> None:
    err_404 = urllib.error.HTTPError("http://x", 404, "not found", None, None)  # type: ignore[arg-type]
    fake = _mock_urlopen_sequence(err_404)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with patch("fetch_jpx_daily.urllib.request.urlopen", side_effect=fake):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            fjd._http_get("http://example/")
    assert exc_info.value.code == 404
    assert fake.calls == 1  # リトライしない


def test_http_get_raises_rate_limited_after_max_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    err_429 = urllib.error.HTTPError("http://x", 429, "rate limited", None, None)  # type: ignore[arg-type]
    fake = _mock_urlopen_sequence(*([err_429] * 3))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with patch("fetch_jpx_daily.urllib.request.urlopen", side_effect=fake):
        with pytest.raises(fjd.RateLimitedError):
            fjd._http_get("http://example/", max_retries=2)


# --- fetch_detailed_daily --------------------------------------------------------


def test_fetch_detailed_daily_downloads_found_dates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = fjd.init_db(tmp_path / "index.db")
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(fjd, "last_complete_day_jst", lambda: datetime.date(2026, 9, 3))

    index_html = '<a href="/x/stq_20260903.pdf">a</a>'
    # 対象ウィンドウ(9/1〜9/3)の外側の日付のみアーカイブに含める
    # → ダウンロードが必要になるのは09-03の1件だけになる
    archive_html = '<a href="/x/stq_20260825.pdf">a</a>'
    pdf_bytes = b"%PDF-fake"

    with patch("fetch_jpx_daily._http_get", side_effect=[
        index_html.encode("utf-8"), archive_html.encode("utf-8"), pdf_bytes,
    ]):
        fjd.fetch_detailed_daily(conn, tmp_path, days_window=3, logger=TEST_LOGGER)

    dest = fjd.detailed_daily_path(tmp_path, "2026-09-03")
    assert dest.read_bytes() == pdf_bytes
    assert fjd.already_done(conn, "2026-09-03", fjd.FORMAT_DETAILED_DAILY)
    # 一覧に無い日付(09-01・09-02)は対象外のまま
    assert not fjd.already_done(conn, "2026-09-02", fjd.FORMAT_DETAILED_DAILY)
    assert not fjd.already_done(conn, "2026-09-01", fjd.FORMAT_DETAILED_DAILY)


def test_fetch_detailed_daily_skips_already_done(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = fjd.init_db(tmp_path / "index.db")
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(fjd, "last_complete_day_jst", lambda: datetime.date(2026, 9, 3))
    fjd.store_progress(conn, "2026-09-03", fjd.FORMAT_DETAILED_DAILY, "done", "https://x/1.pdf", None)

    with patch("fetch_jpx_daily._http_get", side_effect=[b"", b""]) as mock_get:
        fjd.fetch_detailed_daily(conn, tmp_path, days_window=1, logger=TEST_LOGGER)

    # index/archiveの2回だけ呼ばれ、既にdoneな09-03のPDF自体は取得しに行かない
    assert mock_get.call_count == 2


def test_fetch_detailed_daily_force_revisits_done_dates_but_skips_existing_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = fjd.init_db(tmp_path / "index.db")
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(fjd, "last_complete_day_jst", lambda: datetime.date(2026, 9, 3))
    fjd.store_progress(conn, "2026-09-03", fjd.FORMAT_DETAILED_DAILY, "done", "https://x/old.pdf", None)
    index_html = '<a href="/x/stq_20260903.pdf">a</a>'

    with patch("fetch_jpx_daily._http_get", side_effect=[
        index_html.encode("utf-8"), b"",
    ]) as mock_get:
        fjd.fetch_detailed_daily(conn, tmp_path, days_window=1, logger=TEST_LOGGER, force=True)

    # force=Trueでdoneでも対象に含めるが、ファイルが既に存在しない今回のケースでは
    # 実際にダウンロードが必要になるため、index/archive+PDF本体で3回呼ばれる
    assert mock_get.call_count == 3


def test_fetch_detailed_daily_force_skips_download_when_file_already_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = fjd.init_db(tmp_path / "index.db")
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(fjd, "last_complete_day_jst", lambda: datetime.date(2026, 9, 3))
    fjd.store_progress(conn, "2026-09-03", fjd.FORMAT_DETAILED_DAILY, "done", "https://x/old.pdf", None)
    dest = fjd.detailed_daily_path(tmp_path, "2026-09-03")
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"existing-content")
    index_html = '<a href="/x/stq_20260903.pdf">a</a>'

    with patch("fetch_jpx_daily._http_get", side_effect=[index_html.encode("utf-8"), b""]) as mock_get:
        fjd.fetch_detailed_daily(conn, tmp_path, days_window=1, logger=TEST_LOGGER, force=True)

    # index/archiveの2回は呼ばれるが、force=Trueでも既存ファイルは再ダウンロードしない
    # （edinet-dlの--forceと同じ意味）
    assert mock_get.call_count == 2
    assert dest.read_bytes() == b"existing-content"


def test_fetch_detailed_daily_marks_error_on_download_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = fjd.init_db(tmp_path / "index.db")
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(fjd, "last_complete_day_jst", lambda: datetime.date(2026, 9, 3))

    index_html = '<a href="/x/stq_20260903.pdf">a</a>'
    with patch("fetch_jpx_daily._http_get", side_effect=[
        index_html.encode("utf-8"), b"", RuntimeError("boom"),
    ]):
        fjd.fetch_detailed_daily(conn, tmp_path, days_window=1, logger=TEST_LOGGER)

    row = conn.execute(
        "SELECT status, message FROM fetch_progress WHERE period = ? AND format = ?",
        ("2026-09-03", fjd.FORMAT_DETAILED_DAILY),
    ).fetchone()
    assert row is not None
    assert row[0] == "error"


# --- fetch_monthly_recent -----------------------------------------------------------


def test_fetch_monthly_recent_downloads_all_newly_listed_months(tmp_path: Path) -> None:
    conn = fjd.init_db(tmp_path / "index.db")
    current_html = (
        '<a href="/x/tvdivq0000001jan-att/202501.pdf">Jan</a>'
        '<a href="/x/tvdivq0000001jan-att/202502.pdf">Feb</a>'
    )
    pdf_jan, pdf_feb = b"%PDF-jan", b"%PDF-feb"

    with patch("fetch_jpx_daily._http_get", side_effect=[current_html.encode("utf-8"), pdf_jan, pdf_feb]):
        fjd.fetch_monthly_recent(conn, tmp_path, TEST_LOGGER)

    assert fjd.monthly_ohlc_path(tmp_path, "2025-01").read_bytes() == pdf_jan
    assert fjd.monthly_ohlc_path(tmp_path, "2025-02").read_bytes() == pdf_feb
    assert fjd.already_done(conn, "2025-01", fjd.FORMAT_MONTHLY_OHLC)
    assert fjd.already_done(conn, "2025-02", fjd.FORMAT_MONTHLY_OHLC)


def test_fetch_monthly_recent_skips_already_done_non_latest_months(tmp_path: Path) -> None:
    conn = fjd.init_db(tmp_path / "index.db")
    fjd.store_progress(conn, "2025-01", fjd.FORMAT_MONTHLY_OHLC, "done", "https://old/1", None)
    current_html = (
        '<a href="/x/tvdivq0000001jan-att/202501.pdf">Jan</a>'
        '<a href="/x/tvdivq0000001jan-att/202502.pdf">Feb</a>'
    )

    with patch("fetch_jpx_daily._http_get", side_effect=[current_html.encode("utf-8"), b"%PDF-feb"]) as mock_get:
        fjd.fetch_monthly_recent(conn, tmp_path, TEST_LOGGER)

    # 03.html自体の取得 + 未取得の2月分のみ。既にdoneな1月分は再取得されない
    assert mock_get.call_count == 2


def test_fetch_monthly_recent_force_revisits_all_done_months(tmp_path: Path) -> None:
    conn = fjd.init_db(tmp_path / "index.db")
    fjd.store_progress(conn, "2025-01", fjd.FORMAT_MONTHLY_OHLC, "done", "https://old/1", None)
    current_html = (
        '<a href="/x/tvdivq0000001jan-att/202501.pdf">Jan</a>'
        '<a href="/x/tvdivq0000001jan-att/202502.pdf">Feb</a>'
    )

    with patch("fetch_jpx_daily._http_get", side_effect=[
        current_html.encode("utf-8"), b"%PDF-jan", b"%PDF-feb",
    ]) as mock_get:
        fjd.fetch_monthly_recent(conn, tmp_path, TEST_LOGGER, force=True)

    # force=Trueなので既にdoneな1月分も再取得される（03.html + 1月 + 2月 = 3回）
    assert mock_get.call_count == 3
    assert fjd.monthly_ohlc_path(tmp_path, "2025-01").read_bytes() == b"%PDF-jan"


def test_fetch_monthly_recent_always_refetches_the_latest_listed_month(tmp_path: Path) -> None:
    conn = fjd.init_db(tmp_path / "index.db")
    # 最新月（2025-02）は既にdone扱いでも、確定前でまだ更新されうるため常に取得し直す
    fjd.store_progress(conn, "2025-02", fjd.FORMAT_MONTHLY_OHLC, "done", "https://old/2", None)
    dest = fjd.monthly_ohlc_path(tmp_path, "2025-02")
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"old-content")
    current_html = '<a href="/x/tvdivq0000001jan-att/202502.pdf">Feb</a>'

    with patch("fetch_jpx_daily._http_get", side_effect=[current_html.encode("utf-8"), b"new-content"]):
        fjd.fetch_monthly_recent(conn, tmp_path, TEST_LOGGER)

    assert dest.read_bytes() == b"new-content"


def test_fetch_monthly_recent_marks_error_on_download_failure(tmp_path: Path) -> None:
    conn = fjd.init_db(tmp_path / "index.db")
    current_html = '<a href="/x/tvdivq0000001jan-att/202501.pdf">Jan</a>'

    with patch("fetch_jpx_daily._http_get", side_effect=[current_html.encode("utf-8"), RuntimeError("boom")]):
        fjd.fetch_monthly_recent(conn, tmp_path, TEST_LOGGER)

    row = conn.execute(
        "SELECT status, message FROM fetch_progress WHERE period = ? AND format = ?",
        ("2025-01", fjd.FORMAT_MONTHLY_OHLC),
    ).fetchone()
    assert row is not None
    assert row[0] == "error"


def test_fetch_monthly_recent_does_nothing_when_no_links_listed(tmp_path: Path) -> None:
    conn = fjd.init_db(tmp_path / "index.db")

    with patch("fetch_jpx_daily._http_get", return_value=b"<html>no links</html>") as mock_get:
        fjd.fetch_monthly_recent(conn, tmp_path, TEST_LOGGER)

    mock_get.assert_called_once()
    assert conn.execute("SELECT COUNT(*) FROM fetch_progress").fetchone()[0] == 0

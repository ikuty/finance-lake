from __future__ import annotations

import datetime
import io
import sys
import time
import urllib.error
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import backfill_confirmed_archive as bca  # noqa: E402
import fetch_jpx_daily as fjd  # noqa: E402

TEST_LOGGER = bca.setup_logger()


def _make_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


# --- year_month_range ---------------------------------------------------------------


def test_year_month_range_within_single_year() -> None:
    assert bca.year_month_range((2020, 1), (2020, 3)) == [(2020, 1), (2020, 2), (2020, 3)]


def test_year_month_range_spans_year_boundary() -> None:
    assert bca.year_month_range((2019, 11), (2020, 2)) == [
        (2019, 11), (2019, 12), (2020, 1), (2020, 2),
    ]


def test_year_month_range_single_month() -> None:
    assert bca.year_month_range((2020, 1), (2020, 1)) == [(2020, 1)]


# --- legacy_daily_path ---------------------------------------------------------------


def test_legacy_daily_path(tmp_path: Path) -> None:
    path = bca.legacy_daily_path(tmp_path, "2019-12-02")
    assert path == tmp_path / "legacy-daily" / "2019" / "12" / "02" / "stq.pdf"


# --- extract_legacy_zip ---------------------------------------------------------------


def test_extract_legacy_zip_saves_each_daily_pdf(tmp_path: Path) -> None:
    zip_bytes = _make_zip({
        "BO_C0076_20191202.pdf": b"day1",
        "BO_C0076_20191203.pdf": b"day2",
    })
    saved_count = bca.extract_legacy_zip(zip_bytes, tmp_path, TEST_LOGGER)

    assert saved_count == 2
    assert (tmp_path / "legacy-daily" / "2019" / "12" / "02" / "stq.pdf").read_bytes() == b"day1"
    assert (tmp_path / "legacy-daily" / "2019" / "12" / "03" / "stq.pdf").read_bytes() == b"day2"


def test_extract_legacy_zip_raises_when_no_entry_recognized(tmp_path: Path) -> None:
    # 日付を認識できるエントリが1件も無い場合はエラーとする（2026-09-06追加。
    # .tif/.TIF未対応のまま実行し、該当月が0ファイルのまま誤ってdoneになった
    # 実機不具合の再発防止策）
    zip_bytes = _make_zip({"readme.txt": b"not a daily file"})
    with pytest.raises(RuntimeError, match="日付を認識できたものが"):
        bca.extract_legacy_zip(zip_bytes, tmp_path, TEST_LOGGER)


def test_extract_legacy_zip_supports_tif_extension(tmp_path: Path) -> None:
    # 1999年3月以前はTIFF形式（.tif/.TIF混在）
    zip_bytes = _make_zip({
        "19900104.TIF": b"scan1",
        "19900105.tif": b"scan2",
    })
    saved_count = bca.extract_legacy_zip(zip_bytes, tmp_path, TEST_LOGGER)

    assert saved_count == 2
    assert (tmp_path / "legacy-daily" / "1990" / "01" / "04" / "stq.tif").read_bytes() == b"scan1"
    assert (tmp_path / "legacy-daily" / "1990" / "01" / "05" / "stq.tif").read_bytes() == b"scan2"


def test_extract_legacy_zip_does_not_raise_when_some_entries_unrecognized(tmp_path: Path) -> None:
    # 一部だけ未認識のエントリが混ざっていても、他に認識できたものがあればエラーに
    # しない（警告ログのみ）
    zip_bytes = _make_zip({
        "readme.txt": b"not a daily file",
        "BO_C0076_20191202.pdf": b"day1",
    })
    saved_count = bca.extract_legacy_zip(zip_bytes, tmp_path, TEST_LOGGER)
    assert saved_count == 1


def test_extract_legacy_zip_skips_existing_files(tmp_path: Path) -> None:
    dest = bca.legacy_daily_path(tmp_path, "2019-12-02")
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"existing")

    zip_bytes = _make_zip({"BO_C0076_20191202.pdf": b"new-content"})
    saved_count = bca.extract_legacy_zip(zip_bytes, tmp_path, TEST_LOGGER)

    assert saved_count == 0
    assert dest.read_bytes() == b"existing"


# --- backfill_legacy -----------------------------------------------------------------


def test_backfill_legacy_downloads_and_records_progress(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = fjd.init_db(tmp_path / "index.db")
    monkeypatch.setattr(bca, "LEGACY_START_YEAR_MONTH", (2019, 12))
    monkeypatch.setattr(bca, "LEGACY_END_YEAR_MONTH", (2019, 12))
    zip_bytes = _make_zip({"BO_C0076_20191202.pdf": b"day1"})

    with patch("backfill_confirmed_archive._http_get", return_value=zip_bytes):
        bca.backfill_legacy(conn, tmp_path, TEST_LOGGER)

    assert fjd.already_done(conn, "2019-12", bca.FORMAT_LEGACY_DAILY)
    assert (tmp_path / "legacy-daily" / "2019" / "12" / "02" / "stq.pdf").read_bytes() == b"day1"


def test_backfill_legacy_skips_already_done_months(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = fjd.init_db(tmp_path / "index.db")
    monkeypatch.setattr(bca, "LEGACY_START_YEAR_MONTH", (2019, 12))
    monkeypatch.setattr(bca, "LEGACY_END_YEAR_MONTH", (2019, 12))
    fjd.store_progress(conn, "2019-12", bca.FORMAT_LEGACY_DAILY, "done", "https://old", None)

    with patch("backfill_confirmed_archive._http_get") as mock_get:
        bca.backfill_legacy(conn, tmp_path, TEST_LOGGER)

    mock_get.assert_not_called()


def test_backfill_legacy_force_revisits_done_months(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = fjd.init_db(tmp_path / "index.db")
    monkeypatch.setattr(bca, "LEGACY_START_YEAR_MONTH", (2019, 12))
    monkeypatch.setattr(bca, "LEGACY_END_YEAR_MONTH", (2019, 12))
    fjd.store_progress(conn, "2019-12", bca.FORMAT_LEGACY_DAILY, "done", "https://old", None)
    zip_bytes = _make_zip({"BO_C0076_20191202.pdf": b"day1"})

    with patch("backfill_confirmed_archive._http_get", return_value=zip_bytes) as mock_get:
        bca.backfill_legacy(conn, tmp_path, TEST_LOGGER, force=True)

    mock_get.assert_called_once()


def test_backfill_legacy_marks_error_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = fjd.init_db(tmp_path / "index.db")
    monkeypatch.setattr(bca, "LEGACY_START_YEAR_MONTH", (2019, 12))
    monkeypatch.setattr(bca, "LEGACY_END_YEAR_MONTH", (2019, 12))

    with patch("backfill_confirmed_archive._http_get", side_effect=RuntimeError("boom")):
        bca.backfill_legacy(conn, tmp_path, TEST_LOGGER)

    row = conn.execute(
        "SELECT status, message FROM fetch_progress WHERE period = ? AND format = ?",
        ("2019-12", bca.FORMAT_LEGACY_DAILY),
    ).fetchone()
    assert row == ("error", "boom")


# --- backfill_confirmed ---------------------------------------------------------------


def test_backfill_confirmed_downloads_and_records_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = fjd.init_db(tmp_path / "index.db")
    monkeypatch.setattr(bca, "CONFIRMED_START_YEAR_MONTH", (2020, 1))
    monkeypatch.setattr(fjd, "today_jst", lambda: datetime.date(2020, 2, 1))
    monkeypatch.setattr(bca, "today_jst", lambda: datetime.date(2020, 2, 1))

    with patch("backfill_confirmed_archive._http_get", return_value=b"%PDF-content"):
        bca.backfill_confirmed(conn, tmp_path, TEST_LOGGER)

    assert fjd.already_done(conn, "2020-01", fjd.FORMAT_MONTHLY_OHLC)
    assert fjd.monthly_ohlc_path(tmp_path, "2020-01").read_bytes() == b"%PDF-content"


def test_backfill_confirmed_skips_404_without_recording_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = fjd.init_db(tmp_path / "index.db")
    monkeypatch.setattr(bca, "CONFIRMED_START_YEAR_MONTH", (2025, 8))
    monkeypatch.setattr(bca, "today_jst", lambda: datetime.date(2026, 9, 6))
    err_404 = urllib.error.HTTPError("http://x", 404, "not found", None, None)  # type: ignore[arg-type]

    with patch("backfill_confirmed_archive._http_get", side_effect=err_404):
        bca.backfill_confirmed(conn, tmp_path, TEST_LOGGER)

    assert conn.execute("SELECT COUNT(*) FROM fetch_progress").fetchone()[0] == 0


def test_backfill_confirmed_skips_already_done_months(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = fjd.init_db(tmp_path / "index.db")
    monkeypatch.setattr(bca, "CONFIRMED_START_YEAR_MONTH", (2020, 1))
    monkeypatch.setattr(bca, "today_jst", lambda: datetime.date(2020, 2, 1))
    fjd.store_progress(conn, "2020-01", fjd.FORMAT_MONTHLY_OHLC, "done", "https://old", None)

    with patch("backfill_confirmed_archive._http_get") as mock_get:
        bca.backfill_confirmed(conn, tmp_path, TEST_LOGGER)

    mock_get.assert_not_called()


def test_backfill_confirmed_marks_error_on_non_404_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = fjd.init_db(tmp_path / "index.db")
    monkeypatch.setattr(bca, "CONFIRMED_START_YEAR_MONTH", (2020, 1))
    monkeypatch.setattr(bca, "today_jst", lambda: datetime.date(2020, 2, 1))

    with patch("backfill_confirmed_archive._http_get", side_effect=RuntimeError("boom")):
        bca.backfill_confirmed(conn, tmp_path, TEST_LOGGER)

    row = conn.execute(
        "SELECT status, message FROM fetch_progress WHERE period = ? AND format = ?",
        ("2020-01", fjd.FORMAT_MONTHLY_OHLC),
    ).fetchone()
    assert row == ("error", "boom")


# --- _http_get -------------------------------------------------------------------


def test_http_get_retries_on_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    err_429 = urllib.error.HTTPError("http://x", 429, "rate limited", None, None)  # type: ignore[arg-type]
    calls = {"n": 0}

    def fake_urlopen(*args: object, **kwargs: object) -> object:
        calls["n"] += 1
        if calls["n"] == 1:
            raise err_429
        from unittest.mock import MagicMock
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"ok"
        mock_resp.__enter__.return_value = mock_resp
        return mock_resp

    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with patch("backfill_confirmed_archive.urllib.request.urlopen", side_effect=fake_urlopen):
        assert bca._http_get("http://example/") == b"ok"


def test_http_get_raises_immediately_on_404(monkeypatch: pytest.MonkeyPatch) -> None:
    err_404 = urllib.error.HTTPError("http://x", 404, "not found", None, None)  # type: ignore[arg-type]
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with patch("backfill_confirmed_archive.urllib.request.urlopen", side_effect=err_404):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            bca._http_get("http://example/")
    assert exc_info.value.code == 404

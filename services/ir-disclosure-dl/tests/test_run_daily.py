from __future__ import annotations

import json
import logging
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import db  # noqa: E402
import jpx_disclosure_client  # noqa: E402
import run_daily  # noqa: E402
import tdnet_client  # noqa: E402


def test_date_hierarchy_dir_splits_into_three_levels(tmp_path: Path) -> None:
    result = run_daily.date_hierarchy_dir(tmp_path, "2026-08-07")
    assert result == tmp_path / "2026" / "08" / "07"


def test_save_atomic_writes_file_via_tmp_rename(tmp_path: Path) -> None:
    dest = tmp_path / "a" / "b" / "doc.pdf"
    run_daily.save_atomic(dest, b"%PDF-fake")
    assert dest.read_bytes() == b"%PDF-fake"
    assert not (dest.parent / (dest.name + ".tmp")).exists()


def test_load_seed_companies_reads_csv(tmp_path: Path) -> None:
    csv_path = tmp_path / "seed.csv"
    csv_path.write_text(
        "edinet_code,sec_code,filer_name\nE00012,13010,極洋\n", encoding="utf-8"
    )
    conn = db.init_db(tmp_path / "test.db")
    n = run_daily.load_seed_companies(conn, csv_path)
    assert n == 1
    row = db.company_by_jpx_code(conn, "1301")
    assert row is not None
    assert row["edinet_code"] == "E00012"


def test_build_slack_message_reports_success() -> None:
    stats = run_daily.RunStats(tdnet_days_processed=["2026-08-07"], new_events=2, pdf_downloaded=2)
    message = run_daily.build_slack_message(stats)
    assert "✅" in message
    assert "新規検知: 2件" in message
    assert "成功2件" in message


def test_build_slack_message_reports_tdnet_failure() -> None:
    stats = run_daily.RunStats(tdnet_days_failed={"2026-08-07": "network error"})
    message = run_daily.build_slack_message(stats)
    assert "❌" in message
    assert "network error" in message


def test_process_tdnet_watch_records_failure_and_continues(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import datetime
    import logging

    conn = db.init_db(tmp_path / "test.db")
    logger = logging.getLogger("test")

    def fake_run_for_date(conn: object, client: object, date_str: str) -> int:
        if date_str == "2026-08-06":
            raise RuntimeError("boom")
        return 1

    monkeypatch.setattr(tdnet_client, "run_for_date", fake_run_for_date)

    class _FakeClient:
        def get(self, url: str) -> bytes:
            return b""

    stats = run_daily.RunStats()
    dates = [datetime.date(2026, 8, 6), datetime.date(2026, 8, 7)]
    run_daily.process_tdnet_watch(conn, _FakeClient(), dates, False, stats, logger)

    assert stats.tdnet_days_processed == ["2026-08-07"]
    assert "2026-08-06" in stats.tdnet_days_failed


_JPX_HTML = """
<tr id="1101_0">
    <td align="center">2026/09/25</td>
    <td >
        <div class="txtLink2">
            <div class="txtLink2_InnerDiv">
                <a href="/disc/95090/140120260916537214.pdf" target="linkWin9_1">
                    業績予想(連結)の修正に関するお知らせ
                </a>
            </div>
        </div>
    </td>
</tr>
"""


class _FakeJpxClient:
    def get(self, url: str) -> bytes:
        return b""

    def post_form(self, url: str, fields: dict[str, str], referer: str | None = None) -> bytes:
        return _JPX_HTML.encode("utf-8")


def test_process_pdf_downloads_writes_pdf_and_sidecar_metadata_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = db.init_db(tmp_path / "test.db")
    db.upsert_company(conn, "E04500", "95090", "9509", "北海電力")
    db.insert_tdnet_event(
        conn, "2026-09-25", "16:00", "95090", "E04500", "北海電力",
        "業績予想(連結)の修正に関するお知らせ", "forecast_revision",
    )
    conn.commit()

    monkeypatch.setattr(
        jpx_disclosure_client, "download_pdf", lambda client, url: b"%PDF-fake"
    )

    data_dir = tmp_path / "raw"
    stats = run_daily.RunStats()
    logger = logging.getLogger("test")
    run_daily.process_pdf_downloads(conn, _FakeJpxClient(), data_dir, 0.0, stats, logger)

    assert stats.pdf_downloaded == 1
    pdf_path = data_dir / "2026" / "09" / "25" / "E04500" / "140120260916537214.pdf"
    json_path = pdf_path.with_suffix(".json")
    assert pdf_path.read_bytes() == b"%PDF-fake"

    metadata = json.loads(json_path.read_text(encoding="utf-8"))
    assert metadata == {
        "edinet_code": "E04500",
        "sec_code": "95090",
        "company_name": "北海電力",
        "disclosure_kind": "forecast_revision",
        "tdnet_event_date": "2026-09-25",
        "tdnet_kj_time": "16:00",
        "tdnet_title": "業績予想(連結)の修正に関するお知らせ",
        "jpx_disclosure_date": "2026/09/25",
        "jpx_title": "業績予想(連結)の修正に関するお知らせ",
        "pdf_url": "https://www2.jpx.co.jp/disc/95090/140120260916537214.pdf",
    }


class _FakeJpxClientNoMatch:
    """JPX上場会社情報サービス側にまだ対応する開示が掲載されていない状況を模す
    （実機確認、2026-09-29: TDnetの当日開示がJPX側では翌日以降に反映されるケース
    がある）。"""

    def get(self, url: str) -> bytes:
        return b""

    def post_form(self, url: str, fields: dict[str, str], referer: str | None = None) -> bytes:
        return b"<html>no matching row here</html>"


def _insert_forecast_revision_event(conn: sqlite3.Connection, event_date: str) -> None:
    db.upsert_company(conn, "E04500", "95090", "9509", "北海電力")
    db.insert_tdnet_event(
        conn, event_date, "16:00", "95090", "E04500", "北海電力",
        "業績予想(連結)の修正に関するお知らせ", "forecast_revision",
    )
    conn.commit()


def test_process_pdf_downloads_leaves_event_pending_when_jpx_has_no_match_yet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import datetime

    conn = db.init_db(tmp_path / "test.db")
    _insert_forecast_revision_event(conn, "2026-09-28")
    monkeypatch.setattr(tdnet_client, "today_jst", lambda: datetime.date(2026, 9, 29))

    stats = run_daily.RunStats()
    logger = logging.getLogger("test")
    run_daily.process_pdf_downloads(
        conn, _FakeJpxClientNoMatch(), tmp_path / "raw", 0.0, stats, logger
    )

    assert stats.pdf_pending_retry == 1
    assert stats.pdf_skipped == 0
    # まだpdf_downloadsに行が無い = 翌日以降も再試行対象のまま
    assert len(db.pending_tdnet_events(conn)) == 1


def test_process_pdf_downloads_gives_up_after_retry_window_when_no_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import datetime

    conn = db.init_db(tmp_path / "test.db")
    _insert_forecast_revision_event(conn, "2026-09-20")
    monkeypatch.setattr(tdnet_client, "today_jst", lambda: datetime.date(2026, 9, 29))

    stats = run_daily.RunStats()
    logger = logging.getLogger("test")
    run_daily.process_pdf_downloads(
        conn, _FakeJpxClientNoMatch(), tmp_path / "raw", 0.0, stats, logger
    )

    assert stats.pdf_pending_retry == 0
    assert stats.pdf_skipped == 1
    assert len(db.pending_tdnet_events(conn)) == 0


class _FakeJpxClientFetchFails:
    def get(self, url: str) -> bytes:
        return b""

    def post_form(self, url: str, fields: dict[str, str], referer: str | None = None) -> bytes:
        raise RuntimeError("接続エラー")


def test_process_pdf_downloads_leaves_event_pending_on_jpx_fetch_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import datetime

    conn = db.init_db(tmp_path / "test.db")
    _insert_forecast_revision_event(conn, "2026-09-28")
    monkeypatch.setattr(tdnet_client, "today_jst", lambda: datetime.date(2026, 9, 29))

    stats = run_daily.RunStats()
    logger = logging.getLogger("test")
    run_daily.process_pdf_downloads(
        conn, _FakeJpxClientFetchFails(), tmp_path / "raw", 0.0, stats, logger
    )

    assert stats.pdf_pending_retry == 1
    assert stats.pdf_error == 0
    assert len(db.pending_tdnet_events(conn)) == 1

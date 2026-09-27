from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import db  # noqa: E402
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

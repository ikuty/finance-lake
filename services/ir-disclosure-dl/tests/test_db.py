from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import db  # noqa: E402


def test_init_db_creates_tables(tmp_path: Path) -> None:
    conn = db.init_db(tmp_path / "test.db")
    conn.execute("SELECT * FROM companies")
    conn.execute("SELECT * FROM tdnet_fetch_progress")
    conn.execute("SELECT * FROM tdnet_events")
    conn.execute("SELECT * FROM pdf_downloads")


def test_upsert_company_and_lookup(tmp_path: Path) -> None:
    conn = db.init_db(tmp_path / "test.db")
    db.upsert_company(conn, "E00012", "13010", "1301", "株式会社　極洋")
    conn.commit()
    row = db.company_by_jpx_code(conn, "1301")
    assert row is not None
    assert row["edinet_code"] == "E00012"
    assert row["sec_code"] == "13010"


def test_upsert_company_updates_on_conflict(tmp_path: Path) -> None:
    conn = db.init_db(tmp_path / "test.db")
    db.upsert_company(conn, "E1", "10000", "1000", "旧社名")
    db.upsert_company(conn, "E1", "10000", "1000", "新社名")
    conn.commit()
    conn.row_factory = None
    row = conn.execute("SELECT filer_name FROM companies WHERE edinet_code = 'E1'").fetchone()
    assert row == ("新社名",)


def test_already_done_tdnet_false_when_absent(tmp_path: Path) -> None:
    conn = db.init_db(tmp_path / "test.db")
    assert db.already_done_tdnet(conn, "2026-08-07") is False


def test_store_and_check_tdnet_progress(tmp_path: Path) -> None:
    conn = db.init_db(tmp_path / "test.db")
    db.store_tdnet_progress(conn, "2026-08-07", "done", 3, None)
    assert db.already_done_tdnet(conn, "2026-08-07") is True


def test_insert_tdnet_event_returns_id_once(tmp_path: Path) -> None:
    conn = db.init_db(tmp_path / "test.db")
    db.upsert_company(conn, "E1", "10000", "1000", "A社")
    conn.commit()

    id1 = db.insert_tdnet_event(conn, "2026-08-07", "15:00", "10000", "E1", "A社", "決算短信", "kessan_tanshin")
    id2 = db.insert_tdnet_event(conn, "2026-08-07", "15:00", "10000", "E1", "A社", "決算短信", "kessan_tanshin")

    assert id1 is not None
    assert id2 is None  # 重複は無視される


def test_pending_tdnet_events_excludes_already_downloaded(tmp_path: Path) -> None:
    conn = db.init_db(tmp_path / "test.db")
    db.upsert_company(conn, "E1", "10000", "1000", "A社")
    conn.commit()
    event_id = db.insert_tdnet_event(conn, "2026-08-07", "15:00", "10000", "E1", "A社", "決算短信", "kessan_tanshin")
    assert event_id is not None

    assert len(db.pending_tdnet_events(conn)) == 1

    db.record_pdf_download(conn, event_id, "2026/08/07", "決算短信", "https://example.com/a.pdf", "downloaded", "/tmp/a.pdf", None)

    assert len(db.pending_tdnet_events(conn)) == 0

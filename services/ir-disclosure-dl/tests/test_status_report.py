from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import db  # noqa: E402
import status_report  # noqa: E402


def test_generate_report_html_includes_key_sections(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    conn = db.init_db(db_path)
    db.upsert_company(conn, "E00012", "13010", "1301", "極洋")
    conn.commit()
    db.store_tdnet_progress(conn, "2026-08-07", "done", 1, None)
    event_id = db.insert_tdnet_event(conn, "2026-08-07", "15:00", "13010", "E00012", "極洋", "決算短信", "kessan_tanshin")
    assert event_id is not None
    db.record_pdf_download(conn, event_id, "2026/08/07", "決算短信", "https://example.com/a.pdf", "downloaded", "/data/a.pdf", None)

    html, summary = status_report.generate_report_html(db_path)

    assert "2026-08-07" in html
    assert "kessan_tanshin" in html
    assert "downloaded" in html
    assert "検知イベント" in summary


def test_generate_report_html_lists_recent_errors(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    conn = db.init_db(db_path)
    db.upsert_company(conn, "E1", "10000", "1000", "A社")
    conn.commit()
    event_id = db.insert_tdnet_event(conn, "2026-08-07", "15:00", "10000", "E1", "A社", "決算短信", "kessan_tanshin")
    assert event_id is not None
    db.record_pdf_download(conn, event_id, None, None, "", "error", None, "検証用のエラーメッセージ")

    html, _summary = status_report.generate_report_html(db_path)
    assert "検証用のエラーメッセージ" in html

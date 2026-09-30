from __future__ import annotations

import datetime
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
    event_date = status_report.EARLIEST_DATE.isoformat()
    db.store_tdnet_progress(conn, event_date, "done", 1, None)
    event_id = db.insert_tdnet_event(conn, event_date, "15:00", "13010", "E00012", "極洋", "決算短信", "kessan_tanshin")
    assert event_id is not None
    db.record_pdf_download(conn, event_id, "2026/08/07", "決算短信", "https://example.com/a.pdf", "downloaded", "/data/a.pdf", None)

    html, summary = status_report.generate_report_html(db_path)

    assert 'id="grid"' in html
    assert '<td class="done">*</td>' in html
    assert "kessan_tanshin" in html
    assert "downloaded" in html
    assert "検知イベント" in summary


# --- progress_dates ---------------------------------------------------------------------


def test_progress_dates_inclusive_range() -> None:
    dates = status_report.progress_dates(datetime.date(2026, 9, 1), datetime.date(2026, 9, 3))
    assert dates == [datetime.date(2026, 9, 1), datetime.date(2026, 9, 2), datetime.date(2026, 9, 3)]


# --- load_progress_dates_by_status -------------------------------------------------------


def test_load_progress_dates_by_status_filters_by_status(tmp_path: Path) -> None:
    conn = db.init_db(tmp_path / "test.db")
    db.store_tdnet_progress(conn, "2026-09-27", "done", 1, None)
    db.store_tdnet_progress(conn, "2026-09-28", "error", 0, "boom")

    assert status_report.load_progress_dates_by_status(conn, "done") == {datetime.date(2026, 9, 27)}
    assert status_report.load_progress_dates_by_status(conn, "error") == {datetime.date(2026, 9, 28)}


# --- render_progress_grid ----------------------------------------------------------------


def test_render_progress_grid_marks_done_error_and_blank_cells() -> None:
    dates = [datetime.date(2026, 9, 27), datetime.date(2026, 9, 28), datetime.date(2026, 9, 29)]
    html = status_report.render_progress_grid(
        dates, done={datetime.date(2026, 9, 27)}, error={datetime.date(2026, 9, 28)}
    )
    assert '<td class="done">*</td>' in html
    assert '<td class="error">×</td>' in html
    assert html.count("<td></td>") == 1  # 9/29のみ未取得の空欄


def test_render_progress_grid_marks_nonexistent_days_as_na() -> None:
    dates = [datetime.date(2026, 9, d) for d in range(1, 31)]  # 9月は30日まで
    html = status_report.render_progress_grid(dates, done=set())
    assert html.count('<td class="na"></td>') == 1  # 31日目のみ


def test_render_progress_grid_orders_year_months_descending() -> None:
    dates = [datetime.date(2026, 8, 1), datetime.date(2026, 9, 1)]
    html = status_report.render_progress_grid(dates, done=set())
    assert html.index("<th>2026-09</th>") < html.index("<th>2026-08</th>")


def test_generate_report_html_counts_pending_retry(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    conn = db.init_db(db_path)
    db.upsert_company(conn, "E00012", "13010", "1301", "極洋")
    conn.commit()
    # pdf_downloadsに行を作らない = 再試行待ち（run_daily.MAX_PENDING_RETRY_DAYS参照）
    event_id = db.insert_tdnet_event(conn, "2026-08-07", "15:00", "13010", "E00012", "極洋", "決算短信", "kessan_tanshin")
    assert event_id is not None

    html, summary = status_report.generate_report_html(db_path)

    assert "pending（再試行待ち）" in html
    assert "再試行待ち1件" in summary


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

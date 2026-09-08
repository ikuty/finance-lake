from __future__ import annotations

import datetime
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import backfill_report as br  # noqa: E402


def _make_db(tmp_path: Path, rows: list[tuple[str, str]]) -> Path:
    """rows: [(fileDate, status), ...]"""
    db_path = tmp_path / "edinet_index.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE fetch_progress (
            fileDate TEXT PRIMARY KEY, status TEXT, docCount INTEGER, message TEXT, fetchedAt TEXT
        )
    """)
    for file_date, status in rows:
        conn.execute(
            "INSERT INTO fetch_progress (fileDate, status) VALUES (?, ?)",
            (file_date, status),
        )
    conn.commit()
    conn.close()
    return db_path


# --- backfillable_dates ---------------------------------------------------------------


def test_backfillable_dates_inclusive_range() -> None:
    dates = br.backfillable_dates(datetime.date(2026, 9, 1), datetime.date(2026, 9, 3))
    assert dates == [datetime.date(2026, 9, 1), datetime.date(2026, 9, 2), datetime.date(2026, 9, 3)]


def test_backfillable_dates_spans_month_boundary() -> None:
    dates = br.backfillable_dates(datetime.date(2026, 8, 30), datetime.date(2026, 9, 2))
    assert dates == [
        datetime.date(2026, 8, 30), datetime.date(2026, 8, 31),
        datetime.date(2026, 9, 1), datetime.date(2026, 9, 2),
    ]


# --- load_done_dates --------------------------------------------------------------------


def test_load_done_dates_returns_empty_when_db_missing(tmp_path: Path) -> None:
    assert br.load_done_dates(tmp_path / "nonexistent.db") == set()


def test_load_done_dates_filters_by_status(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path, [
        ("2026-09-01", "done"),
        ("2026-09-02", "error"),
    ])
    assert br.load_done_dates(db_path) == {datetime.date(2026, 9, 1)}


# --- render_table -----------------------------------------------------------------------


def test_render_table_marks_done_and_blank_cells() -> None:
    dates = [datetime.date(2026, 9, 1), datetime.date(2026, 9, 2)]
    html = br.render_table(dates, done={datetime.date(2026, 9, 1)})
    assert '<td class="done">*</td>' in html
    assert html.count("<td></td>") == 1


def test_render_table_marks_nonexistent_days_as_na() -> None:
    dates = [datetime.date(2026, 9, d) for d in range(1, 31)]  # 9月は30日まで
    html = br.render_table(dates, done=set())
    assert html.count('<td class="na"></td>') == 1  # 31日目のみ


def test_render_table_orders_year_months_descending() -> None:
    dates = [datetime.date(2026, 8, 1), datetime.date(2026, 9, 1)]
    html = br.render_table(dates, done=set())
    assert html.index("<th>2026-09</th>") < html.index("<th>2026-08</th>")


# --- render_page -----------------------------------------------------------------------


def test_render_page_includes_summary_and_legend() -> None:
    table = br.render_table([datetime.date(2026, 9, 1)], done={datetime.date(2026, 9, 1)})
    html = br.render_page(table, datetime.date(2016, 8, 13))
    assert 'id="summary"' in html
    assert "取得済み" in html
    assert "2016-08-13" in html
    assert table in html


# --- generate_report_html -----------------------------------------------------------------


def test_generate_report_html_returns_html_and_summary(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path, [("2016-08-15", "done")])
    html, summary = br.generate_report_html(db_path)

    assert "<html" in html
    assert 'id="grid"' in html
    assert "日完了" in summary

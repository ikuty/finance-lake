from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import backfill_report as br  # noqa: E402


def _make_db(tmp_path: Path, rows: list[tuple[str, str, str]]) -> Path:
    """rows: [(period, format, status), ...]"""
    db_path = tmp_path / "index.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE fetch_progress (
            period TEXT, format TEXT, status TEXT, sourceUrl TEXT, message TEXT, fetchedAt TEXT,
            PRIMARY KEY (period, format)
        )
    """)
    for period, fmt, status in rows:
        conn.execute(
            "INSERT INTO fetch_progress (period, format, status) VALUES (?, ?, ?)",
            (period, fmt, status),
        )
    conn.commit()
    conn.close()
    return db_path


# --- backfillable_year_months ---------------------------------------------------


def test_backfillable_year_months_starts_at_1981_01() -> None:
    months = br.backfillable_year_months(1981, 3)
    assert months == [(1981, 1), (1981, 2)]


def test_backfillable_year_months_excludes_current_month_and_future() -> None:
    months = br.backfillable_year_months(2026, 9)
    assert months[-1] == (2026, 8)
    assert (2026, 9) not in months


def test_backfillable_year_months_spans_year_boundary() -> None:
    months = br.backfillable_year_months(2020, 2)
    assert months[-3:] == [(2019, 11), (2019, 12), (2020, 1)]


# --- load_done_year_months --------------------------------------------------------


def test_load_done_year_months_returns_empty_when_db_missing(tmp_path: Path) -> None:
    assert br.load_done_year_months(tmp_path / "nonexistent.db") == set()


def test_load_done_year_months_reads_done_rows_only(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path, [
        ("1981-01", br.FORMAT_LEGACY_DAILY, "done"),
        ("1981-02", br.FORMAT_LEGACY_DAILY, "error"),
        ("2020-01", br.FORMAT_MONTHLY_OHLC, "done"),
    ])
    assert br.load_done_year_months(db_path) == {(1981, 1), (2020, 1)}


def test_load_done_year_months_ignores_other_formats(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path, [
        ("2026-09-03", "detailed-daily", "done"),
        ("2025-08", "monthly-ohlc", "done"),
    ])
    # detailed-daily(形式C)はバックフィル対象外なので無視される
    assert br.load_done_year_months(db_path) == {(2025, 8)}


# --- render_html -------------------------------------------------------------------


def test_render_html_marks_done_and_blank_cells() -> None:
    year_months = [(1981, 1), (1981, 2)]
    html = br.render_html(year_months, done={(1981, 1)})
    assert '<td class="done">*</td>' in html
    assert html.count("<td></td>") == 1  # 1981-02は未実施の空欄セル


def test_render_html_marks_out_of_scope_cells_as_na() -> None:
    # 1981年は1月・2月のみ対象（3月以降は対象範囲外）というケース
    year_months = [(1981, 1), (1981, 2)]
    html = br.render_html(year_months, done=set())
    assert html.count('<td class="na"></td>') == 10  # 3月〜12月の10ヶ月分


def test_render_html_includes_summary_script_and_legend() -> None:
    html = br.render_html([(1981, 1)], done={(1981, 1)})
    assert "id=\"summary\"" in html
    assert "バックフィル済み" in html

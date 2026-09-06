from __future__ import annotations

import datetime
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


# --- subtract_months / default_confirmed_cutoff ------------------------------------


def test_subtract_months_within_same_year() -> None:
    assert br.subtract_months(2026, 9, 3) == (2026, 6)


def test_subtract_months_crosses_year_boundary() -> None:
    assert br.subtract_months(2026, 9, 14) == (2025, 7)


def test_default_confirmed_cutoff_uses_default_lag_months() -> None:
    today = datetime.date(2026, 9, 6)
    assert br.default_confirmed_cutoff(today) == br.subtract_months(2026, 9, br.DEFAULT_LAG_MONTHS)


# --- render_monthly_table -----------------------------------------------------------


def test_render_monthly_table_marks_done_and_blank_cells() -> None:
    year_months = [(1981, 1), (1981, 2)]
    html = br.render_monthly_table(year_months, done={(1981, 1)}, display_through_year=1981)
    assert '<td class="done">*</td>' in html
    assert html.count("<td></td>") == 1  # 1981-02は未実施の空欄セル


def test_render_monthly_table_marks_out_of_scope_cells_as_na() -> None:
    # 1981年は1月・2月のみ対象（3月以降は対象範囲外）というケース
    year_months = [(1981, 1), (1981, 2)]
    html = br.render_monthly_table(year_months, done=set(), display_through_year=1981)
    assert html.count('<td class="na"></td>') == 10  # 3月〜12月の10ヶ月分


def test_render_monthly_table_shows_fully_out_of_scope_years_as_all_na_rows() -> None:
    # 直近ラグ期間により2026年が丸ごと対象範囲外(year_monthsに一切登場しない)場合でも、
    # display_through_yearまでの行は表示され、全マスnaになる
    year_months = [(1981, 1)]
    html = br.render_monthly_table(year_months, done=set(), display_through_year=2026)
    assert "<th>2026</th>" in html
    assert "<th>1981</th>" in html


def test_render_monthly_table_orders_years_descending() -> None:
    year_months = [(1981, 1), (1982, 1)]
    html = br.render_monthly_table(year_months, done=set(), display_through_year=1982)
    assert html.index("<th>1982</th>") < html.index("<th>1981</th>")


# --- daily_service_dates / load_done_dates ------------------------------------------


def test_daily_service_dates_spans_single_month() -> None:
    dates = br.daily_service_dates(2026, 9, datetime.date(2026, 9, 5))
    assert dates == [datetime.date(2026, 9, d) for d in range(1, 6)]


def test_daily_service_dates_spans_month_boundary() -> None:
    dates = br.daily_service_dates(2026, 8, datetime.date(2026, 9, 2))
    assert dates[-3:] == [datetime.date(2026, 8, 31), datetime.date(2026, 9, 1), datetime.date(2026, 9, 2)]


def test_load_done_dates_returns_empty_when_db_missing(tmp_path: Path) -> None:
    assert br.load_done_dates(tmp_path / "nonexistent.db", br.FORMAT_DETAILED_DAILY) == set()


def test_load_done_dates_filters_by_format_and_status(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path, [
        ("2026-09-03", br.FORMAT_DETAILED_DAILY, "done"),
        ("2026-09-04", br.FORMAT_DETAILED_DAILY, "error"),
        ("2026-09", br.FORMAT_MONTHLY_OHLC, "done"),
    ])
    assert br.load_done_dates(db_path, br.FORMAT_DETAILED_DAILY) == {datetime.date(2026, 9, 3)}


# --- render_daily_table --------------------------------------------------------------


def test_render_daily_table_marks_done_and_blank_cells() -> None:
    dates = [datetime.date(2026, 9, 1), datetime.date(2026, 9, 2)]
    html = br.render_daily_table(dates, done={datetime.date(2026, 9, 1)})
    assert '<td class="done">*</td>' in html
    assert html.count("<td></td>") == 1


def test_render_daily_table_marks_nonexistent_days_as_na() -> None:
    # 9月は30日までなので、31日目は表の範囲外(na)
    dates = br.daily_service_dates(2026, 9, datetime.date(2026, 9, 30))
    html = br.render_daily_table(dates, done=set())
    assert html.count('<td class="na"></td>') == 1


def test_render_daily_table_orders_year_months_descending() -> None:
    dates = [datetime.date(2026, 8, 1), datetime.date(2026, 9, 1)]
    html = br.render_daily_table(dates, done=set())
    assert html.index("<th>2026-09</th>") < html.index("<th>2026-08</th>")


# --- render_page -----------------------------------------------------------------------


def test_render_page_includes_both_summaries_and_legends() -> None:
    monthly_table = br.render_monthly_table([(1981, 1)], done={(1981, 1)}, display_through_year=1981)
    daily_table = br.render_daily_table([datetime.date(2026, 9, 1)], done={datetime.date(2026, 9, 1)})
    html = br.render_page(daily_table, monthly_table)
    assert 'id="daily-summary"' in html
    assert 'id="monthly-summary"' in html
    assert "バックフィル済み" in html
    assert daily_table in html
    assert monthly_table in html


# --- generate_report_html -----------------------------------------------------------------


def test_generate_report_html_returns_html_and_summary(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path, [
        ("1981-01", br.FORMAT_LEGACY_DAILY, "done"),
        ("2026-09-01", br.FORMAT_DETAILED_DAILY, "done"),
    ])
    html, summary = br.generate_report_html(db_path)

    assert "<html" in html
    assert 'id="monthly-grid"' in html
    assert 'id="daily-grid"' in html
    assert "ヶ月完了" in summary
    assert "日完了" in summary


def test_generate_report_html_respects_end_year_month_override(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path, [("2025-06", br.FORMAT_MONTHLY_OHLC, "done")])
    _, summary_default = br.generate_report_html(db_path)
    _, summary_override = br.generate_report_html(db_path, end_year_month="2025-07")

    # --end-year-monthを指定すると対象範囲(分母)が変わるため、サマリ文字列も変わる
    assert summary_default != summary_override

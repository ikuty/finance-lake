"""実行状況を静的HTMLレポートとして生成する（edinet-dlのbackfill_report.pyと同じ
generate_report_html(db_path) -> (html, summary)という型。日次実行のたびにS3へ
アップロードする運用も同じ）。

TDnet取得状況は、edinet-dlのbackfill_report.pyと同じ年月×日のグリッド形式で表示する
（render_progress_grid、2026-09-30追加）。ただしedinet-dlと異なり、ir-disclosure-dlには
過去分の遡及バックフィル計画が無い（TDnet過去分の遡及取得は行わず、必要になった時点で
JPX上場会社情報サービス側の121ヶ月履歴を使う方針、docs/jpx_disclosure_design.md参照）。
そのためグリッドの起点はedinet-dlのような「初回バックフィル開始日」ではなく、単純な
SERVICE_START_DATE（Mac Miniへの実デプロイ日）とする。
"""
from __future__ import annotations

import datetime
import sqlite3
from collections.abc import Set as AbstractSet
from pathlib import Path

# サービス稼働開始日（2026-09-27、Mac Miniへの実デプロイ日。2026-09-30決定）。
# edinet-dlのEARLIEST_DATEと違い「遡及バックフィルの開始日」ではなく、単に
# このサービスが動き始めた日（それより前はtdnet_fetch_progressに行が存在しない）。
SERVICE_START_DATE = datetime.date(2026, 9, 27)

JST = datetime.timezone(datetime.timedelta(hours=9))

_STYLE = """
body { font-family: -apple-system, "Hiragino Kaku Gothic ProN", Meiryo, monospace; margin: 2em; }
table { border-collapse: collapse; margin-bottom: 2em; }
th, td { border: 1px solid #ccc; padding: 4px 8px; font-size: 0.9em; }
th { background: #eee; }
h2 { margin-top: 2em; }
#grid th, #grid td { width: 18px; height: 16px; text-align: center; padding: 0; font-size: 12px; }
#grid td.done { background: #cdeccd; }
#grid td.error { background: #f3c6c6; color: #a33; }
#grid td.na { background: #eee; }
#grid-legend { color: #666; font-size: 0.85em; margin-top: 4px; }
"""


def today_jst() -> datetime.date:
    return datetime.datetime.now(JST).date()


def last_complete_day_jst() -> datetime.date:
    """tdnet_client.last_complete_day_jst()と同じ理由（当日はまだ検知が完了して
    いない可能性があるため、グリッドの終端は前日とする）。"""
    return today_jst() - datetime.timedelta(days=1)


def progress_dates(start: datetime.date, end: datetime.date) -> list[datetime.date]:
    """startからendまで（両端含む）の日付一覧を返す（edinet-dlのbackfillable_dates
    と同じ）。"""
    dates: list[datetime.date] = []
    d = start
    while d <= end:
        dates.append(d)
        d += datetime.timedelta(days=1)
    return dates


def load_progress_dates_by_status(conn: sqlite3.Connection, status: str) -> set[datetime.date]:
    rows = conn.execute(
        "SELECT event_date FROM tdnet_fetch_progress WHERE status = ?", (status,)
    ).fetchall()
    return {datetime.date.fromisoformat(row[0]) for row in rows}


def render_progress_grid(
    dates_in_scope: list[datetime.date],
    done: set[datetime.date],
    error: AbstractSet[datetime.date] = frozenset(),
) -> str:
    """年月×日のグリッド本体（<table>...</table>）を返す。edinet-dlの
    backfill_report.render_tableと同じ描画ロジック（行=年月、列=日01〜31）。"""
    days_by_year_month: dict[tuple[int, int], set[int]] = {}
    for d in dates_in_scope:
        days_by_year_month.setdefault((d.year, d.month), set()).add(d.day)

    year_months = sorted(days_by_year_month.keys(), reverse=True)

    row_lines = []
    for year, month in year_months:
        present_days = days_by_year_month[(year, month)]
        cells = []
        for day in range(1, 32):
            cell_date = datetime.date(year, month, day) if day in present_days else None
            if cell_date is None:
                cells.append('<td class="na"></td>')
            elif cell_date in done:
                cells.append('<td class="done">*</td>')
            elif cell_date in error:
                cells.append('<td class="error">×</td>')
            else:
                cells.append("<td></td>")
        row_lines.append(f"<tr><th>{year}-{month:02d}</th>{''.join(cells)}</tr>")

    header = "<tr><th></th>" + "".join(f"<th>{d:02d}</th>" for d in range(1, 32)) + "</tr>"
    return f'<table id="grid">\n{header}\n{"".join(row_lines)}\n</table>'


def generate_report_html(db_path: Path) -> tuple[str, str]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    grid_end = last_complete_day_jst()
    grid_dates = progress_dates(SERVICE_START_DATE, grid_end)
    grid_done = load_progress_dates_by_status(conn, "done")
    grid_error = load_progress_dates_by_status(conn, "error")
    grid_table = render_progress_grid(grid_dates, grid_done, grid_error)
    grid_done_count = sum(1 for d in grid_dates if d in grid_done)
    grid_error_count = sum(1 for d in grid_dates if d in grid_error)

    kind_counts = conn.execute(
        "SELECT disclosure_kind, COUNT(*) AS n FROM tdnet_events GROUP BY disclosure_kind"
    ).fetchall()
    total_events = conn.execute("SELECT COUNT(*) AS n FROM tdnet_events").fetchone()["n"]

    status_counts = conn.execute(
        "SELECT status, COUNT(*) AS n FROM pdf_downloads GROUP BY status"
    ).fetchall()

    # pdf_downloadsに行がまだ無いイベント = JPX側に未掲載等で再試行待ち
    # （run_daily.MAX_PENDING_RETRY_DAYSを超えるまでは意図的にDBへ記録しない設計、
    # 詳細はrun_daily.pyのコメント参照）。
    pending_retry_count = conn.execute("""
        SELECT COUNT(*) AS n FROM tdnet_events e
        WHERE NOT EXISTS (SELECT 1 FROM pdf_downloads d WHERE d.tdnet_event_id = e.id)
    """).fetchone()["n"]

    recent_errors = conn.execute("""
        SELECT e.company_name, e.title, e.event_date, d.pdf_url, d.message
        FROM pdf_downloads d JOIN tdnet_events e ON e.id = d.tdnet_event_id
        WHERE d.status = 'error'
        ORDER BY d.id DESC LIMIT 50
    """).fetchall()

    html_parts = [f"<html><head><meta charset='utf-8'><style>{_STYLE}</style></head><body>"]
    html_parts.append("<h1>ir-disclosure-dl 実行状況</h1>")

    html_parts.append(
        f"<h2>TDnet取得状況（{SERVICE_START_DATE.isoformat()}〜{grid_end.isoformat()}）</h2>"
    )
    html_parts.append(
        f"<div>完了: {grid_done_count} / {len(grid_dates)} 日"
        + (f" / 失敗: {grid_error_count} 日" if grid_error_count else "")
        + "</div>"
    )
    html_parts.append(grid_table)
    html_parts.append(
        '<div id="grid-legend">* = 取得済み / × = 取得失敗（要再取得） / '
        "空欄 = 未取得 / 網掛け = 月に存在しない日</div>"
    )

    html_parts.append(f"<h2>検知イベント総数: {total_events}件</h2>")
    html_parts.append("<table><tr><th>種別</th><th>件数</th></tr>")
    for row in kind_counts:
        html_parts.append(f"<tr><td>{row['disclosure_kind']}</td><td>{row['n']}</td></tr>")
    html_parts.append("</table>")

    html_parts.append("<h2>PDF取得状況</h2>")
    html_parts.append("<table><tr><th>状態</th><th>件数</th></tr>")
    for row in status_counts:
        html_parts.append(f"<tr><td>{row['status']}</td><td>{row['n']}</td></tr>")
    html_parts.append(f"<tr><td>pending（再試行待ち）</td><td>{pending_retry_count}</td></tr>")
    html_parts.append("</table>")

    html_parts.append(f"<h2>直近の取得失敗（最大50件）</h2>")
    html_parts.append(
        "<table><tr><th>会社名</th><th>表題</th><th>検知日</th><th>PDF URL</th><th>メッセージ</th></tr>"
    )
    for row in recent_errors:
        html_parts.append(
            f"<tr><td>{row['company_name']}</td><td>{row['title']}</td>"
            f"<td>{row['event_date']}</td><td>{row['pdf_url']}</td><td>{row['message'] or ''}</td></tr>"
        )
    html_parts.append("</table>")

    html_parts.append("</body></html>")
    html = "\n".join(html_parts)

    summary = (
        f"検知イベント{total_events}件 / "
        f"PDF取得 {dict((r['status'], r['n']) for r in status_counts)} / "
        f"再試行待ち{pending_retry_count}件"
    )
    return html, summary

"""実行状況を静的HTMLレポートとして生成する（edinet-dlのbackfill_report.pyと同じ
generate_report_html(db_path) -> (html, summary)という型。日次実行のたびにS3へ
アップロードする運用も同じ）。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

_STYLE = """
body { font-family: -apple-system, "Hiragino Kaku Gothic ProN", Meiryo, monospace; margin: 2em; }
table { border-collapse: collapse; margin-bottom: 2em; }
th, td { border: 1px solid #ccc; padding: 4px 8px; font-size: 0.9em; }
th { background: #eee; }
h2 { margin-top: 2em; }
"""


def generate_report_html(db_path: Path) -> tuple[str, str]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    tdnet_rows = conn.execute(
        "SELECT * FROM tdnet_fetch_progress ORDER BY event_date DESC LIMIT 14"
    ).fetchall()

    kind_counts = conn.execute(
        "SELECT disclosure_kind, COUNT(*) AS n FROM tdnet_events GROUP BY disclosure_kind"
    ).fetchall()
    total_events = conn.execute("SELECT COUNT(*) AS n FROM tdnet_events").fetchone()["n"]

    status_counts = conn.execute(
        "SELECT status, COUNT(*) AS n FROM pdf_downloads GROUP BY status"
    ).fetchall()

    recent_errors = conn.execute("""
        SELECT e.company_name, e.title, e.event_date, d.pdf_url, d.message
        FROM pdf_downloads d JOIN tdnet_events e ON e.id = d.tdnet_event_id
        WHERE d.status = 'error'
        ORDER BY d.id DESC LIMIT 50
    """).fetchall()

    html_parts = [f"<html><head><meta charset='utf-8'><style>{_STYLE}</style></head><body>"]
    html_parts.append("<h1>ir-disclosure-dl 実行状況</h1>")

    html_parts.append("<h2>TDnet取得状況（直近14日）</h2>")
    html_parts.append("<table><tr><th>日付</th><th>状態</th><th>検知件数</th><th>メッセージ</th></tr>")
    for row in tdnet_rows:
        html_parts.append(
            f"<tr><td>{row['event_date']}</td><td>{row['status']}</td>"
            f"<td>{row['event_count']}</td><td>{row['message'] or ''}</td></tr>"
        )
    html_parts.append("</table>")

    html_parts.append(f"<h2>検知イベント総数: {total_events}件</h2>")
    html_parts.append("<table><tr><th>種別</th><th>件数</th></tr>")
    for row in kind_counts:
        html_parts.append(f"<tr><td>{row['disclosure_kind']}</td><td>{row['n']}</td></tr>")
    html_parts.append("</table>")

    html_parts.append("<h2>PDF取得状況</h2>")
    html_parts.append("<table><tr><th>状態</th><th>件数</th></tr>")
    for row in status_counts:
        html_parts.append(f"<tr><td>{row['status']}</td><td>{row['n']}</td></tr>")
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
        f"PDF取得 {dict((r['status'], r['n']) for r in status_counts)}"
    )
    return html, summary

#!/usr/bin/env python3
"""edinet-dlのバックフィル進捗（日付ごとの取得状況）を、年月×日のコンパクトな表で
可視化するHTMLレポートを生成する。設計の詳細はdocs/file_download_design.md参照。

edinet-dlはjpx-daily-pdf-dlと異なり、対象期間全体が単一の粒度（日次、`fileDate`
単位の`fetch_progress`テーブル1つ）のため、表は1つだけで済む（jpxのような
「一回限りのバックフィル対象」と「常設サービス担当」の切り分けは不要。EDINETには
複数形式・確定アーカイブへの移行遅延といった概念が無いため）。

Usage:
    python3 backfill_report.py [--db-path /data/edinet_index.db] [--output backfill_report.html]
"""
from __future__ import annotations

import argparse
import datetime
import sqlite3
from pathlib import Path

JST = datetime.timezone(datetime.timedelta(hours=9))

DEFAULT_DB_PATH = "/data/edinet_index.db"
DEFAULT_OUTPUT = "backfill_report.html"

# 初回バックフィルの開始日（README.md「初回バックフィル」参照、2016-08-13決定）
EARLIEST_DATE = datetime.date(2016, 8, 13)


def today_jst() -> datetime.date:
    return datetime.datetime.now(JST).date()


def last_complete_day_jst() -> datetime.date:
    """サービス本体（fetch_documents.py）と同じ理由で、表の終端は前日とする
    （当日はまだ営業終了前で一覧に現れず、未取得と誤認されるため）。"""
    return today_jst() - datetime.timedelta(days=1)


def backfillable_dates(start: datetime.date, end: datetime.date) -> list[datetime.date]:
    """startからendまで（両端含む）の日付一覧を返す。"""
    dates: list[datetime.date] = []
    d = start
    while d <= end:
        dates.append(d)
        d += datetime.timedelta(days=1)
    return dates


def load_done_dates(db_path: Path) -> set[datetime.date]:
    """fetch_progressテーブルから、status='done'な日付の集合を返す。
    DBファイルが無い場合は空集合を返す。"""
    if not db_path.exists():
        return set()

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT fileDate FROM fetch_progress WHERE status = 'done'").fetchall()
    finally:
        conn.close()

    return {datetime.date.fromisoformat(d) for (d,) in rows}


def render_table(dates_in_scope: list[datetime.date], done: set[datetime.date]) -> str:
    """表本体（<table>...</table>）を返す。行は年月（新しい方が上）、列は日
    （01〜31、月に存在しない日は網掛け）。"""
    days_by_year_month: dict[tuple[int, int], set[int]] = {}
    for d in dates_in_scope:
        days_by_year_month.setdefault((d.year, d.month), set()).add(d.day)

    year_months = sorted(days_by_year_month.keys(), reverse=True)

    row_lines = []
    for year, month in year_months:
        present_days = days_by_year_month[(year, month)]
        cells = []
        for day in range(1, 32):
            if day not in present_days:
                cells.append('<td class="na"></td>')
            elif datetime.date(year, month, day) in done:
                cells.append('<td class="done">*</td>')
            else:
                cells.append("<td></td>")
        row_lines.append(f"<tr><th>{year}-{month:02d}</th>{''.join(cells)}</tr>")

    header = "<tr><th></th>" + "".join(f"<th>{d:02d}</th>" for d in range(1, 32)) + "</tr>"
    return f'<table id="grid">\n{header}\n{"".join(row_lines)}\n</table>'


def render_page(table_html: str, earliest_date: datetime.date) -> str:
    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>edinet-dl バックフィル進捗</title>
<style>
  body {{ font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px;
          margin: 12px; color: #222; background: #fff; }}
  h1 {{ font-size: 13px; font-weight: normal; margin: 0 0 8px; }}
  #summary {{ margin-bottom: 8px; }}
  table {{ border-collapse: collapse; }}
  th, td {{ border: 1px solid #ccc; width: 18px; height: 16px; text-align: center;
            padding: 0; }}
  th {{ background: #f0f0f0; font-weight: normal; }}
  td.done {{ background: #cdeccd; }}
  td.na {{ background: #eee; }}
  #legend {{ margin-top: 6px; color: #666; }}
</style>
</head>
<body>
<h1>edinet-dl バックフィル進捗（{earliest_date.isoformat()}〜）</h1>
<div id="summary"></div>
{table_html}
<div id="legend">* = 取得済み / 空欄 = 未取得 / 網掛け = 月に存在しない日</div>
<script>
  var total = document.querySelectorAll("#grid td:not(.na)").length;
  var doneCount = document.querySelectorAll("#grid td.done").length;
  var pct = total ? (doneCount / total * 100).toFixed(1) : "0.0";
  document.getElementById("summary").textContent =
    "完了: " + doneCount + " / " + total + " 日 (" + pct + "%)";
</script>
</body>
</html>
"""


def generate_report_html(db_path: Path) -> tuple[str, str]:
    """レポートHTML全体と、Slack通知等で使う短いサマリ文字列を生成して返す
    （ファイルには書き込まない）。fetch_documents.pyがSlack通知・S3アップロード
    向けに直接呼び出せるよう用意した（2026-09-08追加）。"""
    end = last_complete_day_jst()
    dates_in_scope = backfillable_dates(EARLIEST_DATE, end)
    done = load_done_dates(db_path)
    table = render_table(dates_in_scope, done)
    html = render_page(table, EARLIEST_DATE)

    done_count = sum(1 for d in dates_in_scope if d in done)
    summary = f"{done_count}/{len(dates_in_scope)}日完了"
    return html, summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db-path", type=str, default=DEFAULT_DB_PATH, help=f"省略時 {DEFAULT_DB_PATH}")
    parser.add_argument("--output", type=str, default=DEFAULT_OUTPUT, help=f"省略時 {DEFAULT_OUTPUT}")
    args = parser.parse_args()

    db_path = Path(args.db_path)
    html, summary = generate_report_html(db_path)

    output_path = Path(args.output)
    output_path.write_text(html, encoding="utf-8")
    print(f"{output_path} に出力しました（{summary}）")


if __name__ == "__main__":
    main()

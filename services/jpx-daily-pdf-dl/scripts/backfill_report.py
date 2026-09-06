#!/usr/bin/env python3
"""形式A（レガシー日次、1981年1月〜2019年12月）・形式B確定済み過去年分（2020年1月〜前月）の
バックフィル進捗を、年×月の表形式で可視化するHTMLレポートを生成する。

対象範囲はサービス本体（fetch_jpx_daily.py）が継続的に担う形式C・形式Bの03.html列挙分
（ローリングウィンドウ）を含まない。この2つには「バックフィル」という概念自体が無い
（常に最新に追従するだけで、確定した過去分が積み上がっていく形式A・Bとは性質が違う）ため。

一回限りの使い捨てバックフィルスクリプト（未実装）が書き込む想定のfetch_progressテーブル
（サービス本体と共有、DB_PATH）を読み取るだけで、書き込みは行わない。バックフィル未実施の
状態でも実行可能（その場合は全マス空欄のレポートが生成される。「残り作業の一覧」として
機能する）。

進捗の粒度は年月（'YYYY-MM'）単位とする（2026-09-06決定）。当初の設計ドキュメントでは
形式Aの粒度を日次（'YYYY-MM-DD'）としていたが、本レポートが年×月のマス目で表示する
ことに合わせ、形式A・Bとも月単位に統一した。形式AのZIPファイルも1ヶ月ぶんが単位で
あり、ダウンロード・展開の成否も月単位でしか意味を持たないため、この粒度変更に実質的な
デメリットは無い。

Usage:
    python3 backfill_report.py [--db-path /data/index.db] [--output backfill_report.html]

DBファイルはサービス本体と共有する（services/jpx-daily-pdf-dl/README.md参照）。Dockerを
経由する必要は無く、sqlite3は標準ライブラリのみで完結するため、Mac Mini上でホストの
Python3から直接DBファイルを指定して実行できる。
"""
from __future__ import annotations

import argparse
import datetime
import sqlite3
from pathlib import Path

JST = datetime.timezone(datetime.timedelta(hours=9))

DEFAULT_DB_PATH = "/data/index.db"
DEFAULT_OUTPUT = "backfill_report.html"

# バックフィル対象の開始年（1980年以前は対象外、2026-09-04決定）
BACKFILL_START_YEAR = 1981
# 形式の境界年（この年以降は形式B、それより前は形式A）
FORMAT_BOUNDARY_YEAR = 2020

FORMAT_LEGACY_DAILY = "legacy-daily"
FORMAT_MONTHLY_OHLC = "monthly-ohlc"


def today_jst() -> datetime.date:
    return datetime.datetime.now(JST).date()


def backfillable_year_months(end_year: int, end_month: int) -> list[tuple[int, int]]:
    """1981-01から(end_year, end_month)の前月までの年月一覧を返す。当月・将来分は
    バックフィル対象外（JPX側で未確定、サービス本体側の担当のため）。"""
    months: list[tuple[int, int]] = []
    year, month = BACKFILL_START_YEAR, 1
    while (year, month) < (end_year, end_month):
        months.append((year, month))
        month += 1
        if month > 12:
            month = 1
            year += 1
    return months


def load_done_year_months(db_path: Path) -> set[tuple[int, int]]:
    """fetch_progressテーブルから、status='done'な(year, month)の集合を返す。
    DBファイルが無い場合（バックフィル未実施）は空集合を返す。"""
    if not db_path.exists():
        return set()

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT period FROM fetch_progress WHERE format IN (?, ?) AND status = 'done'",
            (FORMAT_LEGACY_DAILY, FORMAT_MONTHLY_OHLC),
        ).fetchall()
    finally:
        conn.close()

    done: set[tuple[int, int]] = set()
    for (period,) in rows:
        year_str, month_str = period.split("-")[:2]
        done.add((int(year_str), int(month_str)))
    return done


def render_html(year_months: list[tuple[int, int]], done: set[tuple[int, int]]) -> str:
    in_scope = set(year_months)
    # 新しい年ほど上に来るよう降順（直近の進捗を確認する頻度の方が高いため）
    years = sorted({y for y, _ in year_months}, reverse=True)

    row_lines = []
    for year in years:
        cells = []
        for month in range(1, 13):
            if (year, month) not in in_scope:
                cells.append('<td class="na"></td>')
            elif (year, month) in done:
                cells.append('<td class="done">*</td>')
            else:
                cells.append("<td></td>")
        # 形式境界（B→A、降順なので2020年の次に来る2019年の上に線を引く）を罫線で示す
        row_class = ' class="boundary"' if year == FORMAT_BOUNDARY_YEAR - 1 else ""
        row_lines.append(f"<tr{row_class}><th>{year}</th>{''.join(cells)}</tr>")

    header = "<tr><th></th>" + "".join(f"<th>{m:02d}</th>" for m in range(1, 13)) + "</tr>"

    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>jpx-daily-pdf-dl バックフィル進捗</title>
<style>
  body {{ font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px;
          margin: 12px; color: #222; background: #fff; }}
  h1 {{ font-size: 13px; font-weight: normal; margin: 0 0 6px; }}
  #summary {{ margin-bottom: 8px; }}
  table {{ border-collapse: collapse; }}
  th, td {{ border: 1px solid #ccc; width: 20px; height: 16px; text-align: center;
            padding: 0; }}
  th {{ background: #f0f0f0; font-weight: normal; }}
  td.done {{ background: #cdeccd; }}
  td.na {{ background: #eee; }}
  tr.boundary th, tr.boundary td {{ border-top: 2px solid #333; }}
  #legend {{ margin-top: 8px; color: #666; }}
</style>
</head>
<body>
<h1>jpx-daily-pdf-dl バックフィル進捗（形式A: 1981-2019 / 形式B確定済み過去年分: 2020-前月）</h1>
<div id="summary"></div>
<table id="grid">
{header}
{"".join(row_lines)}
</table>
<div id="legend">* = バックフィル済み / 空欄 = 未実施 / 網掛け = 対象範囲外</div>
<script>
  var total = document.querySelectorAll("#grid td:not(.na)").length;
  var doneCount = document.querySelectorAll("#grid td.done").length;
  var pct = total ? (doneCount / total * 100).toFixed(1) : "0.0";
  document.getElementById("summary").textContent =
    "完了: " + doneCount + " / " + total + " ヶ月 (" + pct + "%)";
</script>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db-path", type=str, default=DEFAULT_DB_PATH, help=f"省略時 {DEFAULT_DB_PATH}")
    parser.add_argument("--output", type=str, default=DEFAULT_OUTPUT, help=f"省略時 {DEFAULT_OUTPUT}")
    args = parser.parse_args()

    today = today_jst()
    year_months = backfillable_year_months(today.year, today.month)
    done = load_done_year_months(Path(args.db_path))
    html = render_html(year_months, done)

    output_path = Path(args.output)
    output_path.write_text(html, encoding="utf-8")

    done_count = sum(1 for ym in year_months if ym in done)
    print(f"{output_path} に出力しました（{done_count}/{len(year_months)}ヶ月完了）")


if __name__ == "__main__":
    main()

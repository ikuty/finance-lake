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

直近の期間（既定で当月から20ヶ月分）は月次バックフィルの「対象範囲外」として扱う
（2026-09-06修正）。形式Cが直近13ヶ月程度をローリングウィンドウで継続的に担当して
おり、かつ形式Bの確定アーカイブへの移行にも遅れがあるため、この期間はそもそも
一回限りのバックフィルの対象ではなく、常設サービス側の担当である。単純に「今月
より前はすべて対象」としてしまうと、常設サービスの担当範囲がバックフィル未実施で
あるかのように誤表示されてしまう。20ヶ月という値は、実機確認（2026-09-06、
`data/{yyyymm}.pdf`への直接アクセス）で確定アーカイブが実際に2024年12月までしか
無い（2025年1月分は404）と判明したことに基づく実測値（この時点で当月2026年9月との
差が20ヶ月）。正確な確定境界はJPX側の移行状況次第で変動し今後もこのペースで進むとは
限らないため、あくまで目安であり、`--end-year-month`で上書きできるようにする。

この「対象範囲外」の期間（常設サービス担当分）についても、どこまで実際に取得済み
かを別途確認できるよう、月次の表とは分離した日次の表を上に表示する（2026-09-06
追加）。対象は形式C（詳細日次）のみ（形式Bの03.html列挙分は月次粒度のため、月次の
表と同じ粒度でしか意味を持たない）。日次の表の範囲は、月次バックフィルの対象範囲外
となる年月の1日から、前日（`last_complete_day_jst()`）までとする。

Usage:
    python3 backfill_report.py [--db-path /data/index.db] [--output backfill_report.html]
                                [--end-year-month YYYY-MM]

DBファイルはサービス本体と共有する（services/jpx-daily-pdf-dl/README.md参照）。Dockerを
経由する必要は無く、sqlite3は標準ライブラリのみで完結するため、Mac Mini上でホストの
Python3から直接DBファイルを指定して実行できる。
"""
from __future__ import annotations

import argparse
import calendar
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
# 当月から遡って何ヶ月分を「確定境界が不明なため対象範囲外」とするかの既定値。
# 実機確認（2026-09-06）で、確定アーカイブが実際に2024年12月までしか無い
# （2025年1月分はdata/{yyyymm}.pdfが404）ことが判明した。この時点で当月
# （2026年9月）との差は20ヶ月。当初「形式Cの13ヶ月ローリングウィンドウ＋
# 1ヶ月の余裕」で見積もっていた14ヶ月は、実際の遅れ（約1年半〜2年）より
# 短すぎた。あくまで目安であり、JPX側の確定移行が今後この通りのペースで
# 進むとは限らないため、正確な境界は--end-year-monthで上書きする。
DEFAULT_LAG_MONTHS = 20

FORMAT_LEGACY_DAILY = "legacy-daily"
FORMAT_MONTHLY_OHLC = "monthly-ohlc"
FORMAT_DETAILED_DAILY = "detailed-daily"


def today_jst() -> datetime.date:
    return datetime.datetime.now(JST).date()


def last_complete_day_jst() -> datetime.date:
    """サービス本体(fetch_jpx_daily.py)と同じ理由で、日次表の終端は前日とする
    （当日はまだ営業終了前で一覧に現れず、未実施と誤認されるため）。"""
    return today_jst() - datetime.timedelta(days=1)


def subtract_months(year: int, month: int, n: int) -> tuple[int, int]:
    total = year * 12 + (month - 1) - n
    return total // 12, total % 12 + 1


def default_confirmed_cutoff(today: datetime.date) -> tuple[int, int]:
    """バックフィル対象とみなす年月の上限（この年月は含まず、その前月まで）の既定値。
    当月からDEFAULT_LAG_MONTHSヶ月遡った年月を返す。"""
    return subtract_months(today.year, today.month, DEFAULT_LAG_MONTHS)


def backfillable_year_months(end_year: int, end_month: int) -> list[tuple[int, int]]:
    """1981-01から(end_year, end_month)の前月までの年月一覧を返す。それ以降
    （既定では直近20ヶ月）はバックフィル対象外（形式Cのローリングウィンドウ・
    形式Bの確定移行待ちとして常設サービス側が担当するため）。"""
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


def daily_service_dates(start_year: int, start_month: int, end_date: datetime.date) -> list[datetime.date]:
    """常設サービス担当分（月次バックフィルの対象範囲外）の日次進捗を確認するための
    日付一覧。(start_year, start_month)の1日からend_dateまでの実在する日付を返す。"""
    dates: list[datetime.date] = []
    year, month = start_year, start_month
    while True:
        _, days_in_month = calendar.monthrange(year, month)
        for day in range(1, days_in_month + 1):
            d = datetime.date(year, month, day)
            if d > end_date:
                return dates
            dates.append(d)
        month += 1
        if month > 12:
            month = 1
            year += 1


def load_done_dates(db_path: Path, fmt: str) -> set[datetime.date]:
    """fetch_progressテーブルから、指定formatでstatus='done'な日付の集合を返す。
    DBファイルが無い場合は空集合を返す。"""
    if not db_path.exists():
        return set()

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT period FROM fetch_progress WHERE format = ? AND status = 'done'",
            (fmt,),
        ).fetchall()
    finally:
        conn.close()

    return {datetime.date.fromisoformat(period) for (period,) in rows}


def render_monthly_table(
    year_months: list[tuple[int, int]], done: set[tuple[int, int]], display_through_year: int
) -> str:
    """月次バックフィル（形式A・形式B確定済み過去年分）の表本体（<table>...</table>）を返す。"""
    in_scope = set(year_months)
    # 新しい年ほど上に来るよう降順（直近の進捗を確認する頻度の方が高いため）。
    # display_through_yearまでは、その年が丸ごと対象範囲外（全マスna）でも行を表示する
    # （常設サービスの担当範囲であることが一目で分かるようにするため。2026-09-06修正）。
    years = list(range(display_through_year, BACKFILL_START_YEAR - 1, -1))

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
    return f'<table id="monthly-grid">\n{header}\n{"".join(row_lines)}\n</table>'


def render_daily_table(dates_in_scope: list[datetime.date], done: set[datetime.date]) -> str:
    """常設サービス担当分（形式Cの日次進捗）の表本体（<table>...</table>）を返す。
    行は年月（新しい方が上）、列は日（01〜31、月に存在しない日はna）。"""
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
    return f'<table id="daily-grid">\n{header}\n{"".join(row_lines)}\n</table>'


def render_page(daily_table_html: str, monthly_table_html: str) -> str:
    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>jpx-daily-pdf-dl バックフィル進捗</title>
<style>
  body {{ font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px;
          margin: 12px; color: #222; background: #fff; }}
  h1 {{ font-size: 13px; font-weight: normal; margin: 0 0 14px; }}
  h2 {{ font-size: 12px; font-weight: normal; margin: 0 0 4px; }}
  section {{ margin-bottom: 16px; }}
  .summary {{ margin-bottom: 6px; }}
  table {{ border-collapse: collapse; }}
  th, td {{ border: 1px solid #ccc; width: 18px; height: 16px; text-align: center;
            padding: 0; }}
  th {{ background: #f0f0f0; font-weight: normal; }}
  td.done {{ background: #cdeccd; }}
  td.na {{ background: #eee; }}
  #monthly-grid th, #monthly-grid td {{ width: 20px; }}
  tr.boundary th, tr.boundary td {{ border-top: 2px solid #333; }}
  .legend {{ margin-top: 6px; color: #666; }}
</style>
</head>
<body>
<h1>jpx-daily-pdf-dl バックフィル進捗</h1>

<section>
<h2>常設サービス担当分（形式C・日次、月次バックフィルの対象範囲外の期間）</h2>
<div class="summary" id="daily-summary"></div>
{daily_table_html}
<div class="legend">* = 取得済み / 空欄 = 未取得 / 網掛け = 表の範囲外（月が存在しない日、または前日より後）</div>
</section>

<section>
<h2>一回限りのバックフィル対象（形式A: 1981-2019 / 形式B確定済み過去年分: 2020-前月）</h2>
<div class="summary" id="monthly-summary"></div>
{monthly_table_html}
<div class="legend">
* = バックフィル済み / 空欄 = 未実施 / 網掛け = 対象範囲外
（直近の一定期間は形式C・形式Bの03.html列挙分として常設サービスが担当するため対象外。
確定境界は目安であり、正確には--end-year-monthで調整する。上の日次の表で確認できる）
</div>
</section>

<script>
  function summarize(gridId, unitLabel) {{
    var total = document.querySelectorAll("#" + gridId + " td:not(.na)").length;
    var doneCount = document.querySelectorAll("#" + gridId + " td.done").length;
    var pct = total ? (doneCount / total * 100).toFixed(1) : "0.0";
    return "完了: " + doneCount + " / " + total + " " + unitLabel + " (" + pct + "%)";
  }}
  document.getElementById("daily-summary").textContent = summarize("daily-grid", "日");
  document.getElementById("monthly-summary").textContent = summarize("monthly-grid", "ヶ月");
</script>
</body>
</html>
"""


def generate_report_html(db_path: Path, end_year_month: str | None = None) -> tuple[str, str]:
    """バックフィル進捗レポートのHTML全体と、Slack通知等で使う短いサマリ文字列を
    生成して返す（ファイルには書き込まない）。fetch_jpx_daily.py（日次サービス
    本体）がSlack通知・S3アップロード向けに直接呼び出せるよう、main()のロジックから
    切り出した（2026-09-06追加）。"""
    today = today_jst()
    if end_year_month:
        end_year_str, end_month_str = end_year_month.split("-")
        end_year, end_month = int(end_year_str), int(end_month_str)
    else:
        end_year, end_month = default_confirmed_cutoff(today)

    year_months = backfillable_year_months(end_year, end_month)
    monthly_done = load_done_year_months(db_path)
    monthly_table = render_monthly_table(year_months, monthly_done, display_through_year=today.year)

    last_complete_day = last_complete_day_jst()
    dates_in_scope = daily_service_dates(end_year, end_month, last_complete_day)
    daily_done = load_done_dates(db_path, FORMAT_DETAILED_DAILY)
    daily_table = render_daily_table(dates_in_scope, daily_done)

    html = render_page(daily_table, monthly_table)

    monthly_done_count = sum(1 for ym in year_months if ym in monthly_done)
    daily_done_count = sum(1 for d in dates_in_scope if d in daily_done)
    summary = (
        f"月次: {monthly_done_count}/{len(year_months)}ヶ月完了、"
        f"日次: {daily_done_count}/{len(dates_in_scope)}日完了"
    )
    return html, summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db-path", type=str, default=DEFAULT_DB_PATH, help=f"省略時 {DEFAULT_DB_PATH}")
    parser.add_argument("--output", type=str, default=DEFAULT_OUTPUT, help=f"省略時 {DEFAULT_OUTPUT}")
    parser.add_argument(
        "--end-year-month", type=str, default=None,
        help=(
            "バックフィル対象とみなす上限（YYYY-MM、この年月は含まずその前月まで）。"
            f"省略時は当月から{DEFAULT_LAG_MONTHS}ヶ月遡った年月（形式Cのローリング"
            "ウィンドウ・形式Bの確定移行待ち期間の目安）"
        ),
    )
    args = parser.parse_args()

    db_path = Path(args.db_path)
    html, summary = generate_report_html(db_path, args.end_year_month)

    output_path = Path(args.output)
    output_path.write_text(html, encoding="utf-8")
    print(f"{output_path} に出力しました（{summary}）")


if __name__ == "__main__":
    main()

"""TDnet（適時開示情報閲覧サービス、release.tdnet.info）の日次開示一覧を取得し、
決算短信・業績予想の修正に該当する行のうち、対象銘柄（companiesテーブル）に一致
するものだけを抽出する。TDnet側のPDFリンクは一切保持・取得しない
（TDnetは開示PDFそのものの二次利用・再配布を禁止しているため、ここでは
「いつ・どの銘柄が・何を開示したか」というメタデータの検知のみに用いる。
実際のPDF取得は東証上場会社情報サービス側から行う。詳細は
docs/jpx_disclosure_design.md参照）。

実機確認済みのHTML構造（2026-09-27時点）:
  URL: https://www.release.tdnet.info/inbs/I_list_{page:03d}_{YYYYMMDD}.html
  1日で複数ページに分かれる（1ページ100件、404になったら打ち切り）。
  各行は<td class="{odd|even}new-{L|M|R} kjXxx">の形式（oddnew/evennewが交互）。
  kjCodeはEDINETのsec_codeと同じ5桁形式（例: "94700"、末尾は株式種別）。
"""
from __future__ import annotations

import datetime
import re
import sqlite3
from dataclasses import dataclass
from typing import Protocol

import db

JST = datetime.timezone(datetime.timedelta(hours=9))


class HttpClientLike(Protocol):
    """このモジュールが必要とする最小限のHTTPクライアントインターフェース
    （http_client.RetryingHttpClientの構造的部分型）。テストではこれを満たす
    フェイクに差し替える。"""

    def get(self, url: str) -> bytes: ...

LIST_URL_TEMPLATE = "https://www.release.tdnet.info/inbs/I_list_{page:03d}_{date}.html"

DISCLOSURE_KEYWORDS: dict[str, str] = {
    "決算短信": "kessan_tanshin",
    "業績予想の修正": "forecast_revision",
}

# "業績予想の修正"は連続する部分文字列としては一致しないタイトル表記が実在する
# （実機確認、2026-09-27: 北海電力(95090)が同日に出した別の開示
# 「業績予想(連結)の修正に関するお知らせ」は、"業績予想"と"の修正"の間に"(連結)"が
# 挿入されており、連続文字列マッチでは見逃す）。そのためforecast_revisionのみ、
# 両方の語が（連続していなくても）含まれていればよいという緩い判定にする。
# 決算短信は同種の表記ゆれが実データ上見当たらなかったため、連続文字列マッチのまま
# とする。
_FORECAST_REVISION_REQUIRED_WORDS = ("業績予想", "修正")

_ROW_PATTERN = re.compile(
    r'<td class="[^"]*kjTime[^"]*"[^>]*>\s*(?P<time>[^<]*?)\s*</td>\s*'
    r'<td class="[^"]*kjCode[^"]*"[^>]*>\s*(?P<code>[^<]*?)\s*</td>\s*'
    r'<td class="[^"]*kjName[^"]*"[^>]*>\s*(?P<name>[^<]*?)\s*</td>\s*'
    r'<td class="[^"]*kjTitle[^"]*"[^>]*>.*?<a[^>]*>(?P<title>.*?)</a>',
    re.S,
)


@dataclass(frozen=True)
class RawTdnetRow:
    kj_time: str
    tdnet_code: str
    company_name: str
    title: str


@dataclass(frozen=True)
class MatchedEvent:
    kj_time: str
    tdnet_code: str
    company_name: str
    title: str
    edinet_code: str
    disclosure_kind: str


def fetch_list_page(client: HttpClientLike, date_str: str, page: int) -> bytes | None:
    """date_strはISO形式(YYYY-MM-DD)を受け取るが、TDnetのURLはハイフン無しの
    YYYYMMDD形式を要求するため、ここで変換する（2026-09-27実機検証で発覚:
    ISO形式のまま渡すと1ページ目から404になり、検知件数が常に0件になっていた）。"""
    compact_date = date_str.replace("-", "")
    url = LIST_URL_TEMPLATE.format(page=page, date=compact_date)
    try:
        return client.get(url)
    except RuntimeError as e:
        if "status=404" in str(e) or "HTTPエラー status=404" in str(e):
            return None
        raise


def parse_list_page(html: bytes) -> list[RawTdnetRow]:
    text = html.decode("utf-8", errors="ignore")
    rows = []
    for m in _ROW_PATTERN.finditer(text):
        rows.append(
            RawTdnetRow(
                kj_time=m.group("time").strip(),
                tdnet_code=m.group("code").strip(),
                company_name=m.group("name").strip(),
                title=m.group("title").strip(),
            )
        )
    return rows


def _match_disclosure_kind(title: str) -> str | None:
    if "決算短信" in title:
        return "kessan_tanshin"
    if all(word in title for word in _FORECAST_REVISION_REQUIRED_WORDS):
        return "forecast_revision"
    return None


def filter_and_match(rows: list[RawTdnetRow], conn: sqlite3.Connection) -> list[MatchedEvent]:
    matched = []
    for row in rows:
        kind = _match_disclosure_kind(row.title)
        if kind is None:
            continue
        company = conn.execute(
            "SELECT edinet_code FROM companies WHERE sec_code = ?", (row.tdnet_code,)
        ).fetchone()
        if company is None:
            continue
        matched.append(
            MatchedEvent(
                kj_time=row.kj_time,
                tdnet_code=row.tdnet_code,
                company_name=row.company_name,
                title=row.title,
                edinet_code=company[0],
                disclosure_kind=kind,
            )
        )
    return matched


def run_for_date(conn: sqlite3.Connection, client: HttpClientLike, date_str: str) -> int:
    """対象日の一覧取得〜フィルタ・照合〜tdnet_events保存〜tdnet_fetch_progress更新まで
    一括で行う。戻り値は新規に一致・保存したイベント件数。"""
    all_rows: list[RawTdnetRow] = []
    page = 1
    while True:
        html = fetch_list_page(client, date_str, page)
        if html is None:
            break
        page_rows = parse_list_page(html)
        if not page_rows:
            break
        all_rows.extend(page_rows)
        page += 1

    matched = filter_and_match(all_rows, conn)
    inserted = 0
    for event in matched:
        event_id = db.insert_tdnet_event(
            conn, date_str, event.kj_time, event.tdnet_code, event.edinet_code,
            event.company_name, event.title, event.disclosure_kind,
        )
        if event_id is not None:
            inserted += 1

    db.store_tdnet_progress(conn, date_str, "done", inserted, None)
    return inserted


def today_jst() -> datetime.date:
    """JSTでの「今日」を返す。datetime.date.today()はコンテナのシステムTZ（通常UTC）に
    依存するため使わない（edinet-dlと同じ理由、fetch_documents.py参照）。"""
    return datetime.datetime.now(JST).date()


def last_complete_day_jst() -> datetime.date:
    """取得対象として安全な「最後の完結した日」＝前日を返す（edinet-dlと同じ理由）。"""
    return today_jst() - datetime.timedelta(days=1)

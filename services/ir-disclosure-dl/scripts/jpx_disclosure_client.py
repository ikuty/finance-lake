"""東証上場会社情報サービス（www2.jpx.co.jp/tseHpFront/）から、銘柄コード指定で
決算短信・業績予想の修正等のPDFへの直接リンクを取得する。

このサービス固有の免責事項（jpx.co.jp/listing/co-search/01.html）は
「東証は本サービスで公開している情報の利用を制限しておりません」と明記しており、
サイト全体の利用規約（二次利用・再配信を原則禁止）とは別に、この特定サービスの
情報については利用が制限されていない（2026-09-27確認、詳細はdocs/
jpx_disclosure_design.mdの経緯参照）。TDnet自体は開示PDFの二次利用・再配布を
禁止しているため、TDnetは「いつ・どの銘柄が開示したか」の検知のみに使い、
PDF本体はこちらのサービスから取得する設計にしている。

実機解析済みのフォーム送信フロー（Struts系のdispatchパターン、2026-09-27確認）:
  1. GET  /tseHpFront/JJK010010Action.do?Show=Show          → セッションCookie確立
  2. POST /tseHpFront/JJK010010Action.do                     → 銘柄コードで検索
  3. POST /tseHpFront/JJK010030Action.do                     → 銘柄詳細（決算短信
     等の全履歴とPDFへの直接リンクが1レスポンスに含まれる）
いずれもスクリプトからの機械的アクセス（curlのデフォルトUA含む）でボット判定なし
に200 OKで応答することを実機確認済み。
"""
from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from typing import Protocol


class HttpClientLike(Protocol):
    """このモジュールが必要とする最小限のHTTPクライアントインターフェース
    （http_client.RetryingHttpClientの構造的部分型）。テストではこれを満たす
    フェイクに差し替える。"""

    def get(self, url: str) -> bytes: ...
    def post_form(self, url: str, fields: dict[str, str], referer: str | None = None) -> bytes: ...


SEARCH_PAGE_URL = "https://www2.jpx.co.jp/tseHpFront/JJK010010Action.do?Show=Show"
SEARCH_POST_URL = "https://www2.jpx.co.jp/tseHpFront/JJK010010Action.do"
DETAIL_POST_URL = "https://www2.jpx.co.jp/tseHpFront/JJK010030Action.do"
DISC_HOST = "https://www2.jpx.co.jp"

# 適時開示情報[決算情報]カテゴリの行だけがid="1101_N"を持つ（実機確認、2026-09-27。
# 他カテゴリ[決定事実/発生事実]等は1102・1105・1106・1107・1109等の別プレフィックス
# を使うため、外側のtable境界を追わずこのidだけで安全に絞り込める）。
_ROW_PATTERN = re.compile(
    r'<tr id="1101_\d+">\s*'
    r'<td align="center">\s*(?P<date>[\d/]+)\s*</td>\s*'
    r'<td\s*>\s*'
    r'<div class="txtLink2">\s*'
    r'<div class="txtLink2_InnerDiv">\s*'
    r'<a href="(?P<href>[^"]+)"[^>]*>\s*(?P<title>.*?)\s*</a>',
    re.S,
)


@dataclass(frozen=True)
class JpxDisclosure:
    disclosure_date: str  # YYYY/MM/DD (JPXの表示形式のまま)
    title: str
    pdf_url: str


def fetch_company_page(client: HttpClientLike, sec_code: str) -> str:
    """sec_code(EDINETと同じ5桁形式、例'13010')を指定して検索→詳細ページ取得までを行う。
    セッションはclient内蔵のCookie jarで維持される。"""
    client.get(SEARCH_PAGE_URL)
    client.post_form(
        SEARCH_POST_URL,
        {
            "ListShow": "ListShow",
            "sniMtGmnId": "",
            "mgrMiTxtBx": "",
            "eqMgrCd": sec_code,
            "dspSsuPd": "10",
        },
        referer=SEARCH_PAGE_URL,
    )
    body = client.post_form(
        DETAIL_POST_URL,
        {
            "BaseJh": "BaseJh",
            "lstDspPg": "1",
            "dspGs": "10",
            "souKnsu": "1",
            "sniMtGmnId": "JJK010010",
            "dspJnKbn": "0",
            "dspJnKmkNo": "0",
            "mgrCd": sec_code,
            "jjHisiFlg": "1",
        },
        referer=SEARCH_POST_URL,
    )
    return body.decode("utf-8", errors="ignore")


def parse_kessan_disclosures(html: str) -> list[JpxDisclosure]:
    disclosures = []
    for m in _ROW_PATTERN.finditer(html):
        href = m.group("href")
        if not href.startswith("http"):
            href = DISC_HOST + href
        disclosures.append(
            JpxDisclosure(disclosure_date=m.group("date"), title=m.group("title"), pdf_url=href)
        )
    return disclosures


_DISCLOSURE_KIND_KEYWORDS = {"kessan_tanshin": "決算短信", "forecast_revision": "業績予想の修正"}


def select_matching_disclosure(
    disclosures: list[JpxDisclosure], event_date: str, disclosure_kind: str, max_days_diff: int = 3
) -> JpxDisclosure | None:
    """TDnetで検知したイベント(event_date, disclosure_kind)に対応するJPX側の開示を選ぶ。
    同じ種別のキーワードを含み、開示日がevent_dateに最も近い（前後max_days_diff日以内）
    ものを採用する。JPXの開示日はTDnetのevent_dateと通常一致するが、日付跨ぎ等の
    ずれを許容するため多少の幅を持たせる。"""
    keyword = _DISCLOSURE_KIND_KEYWORDS[disclosure_kind]
    event_dt = datetime.date.fromisoformat(event_date)

    best: JpxDisclosure | None = None
    best_diff: int | None = None
    for d in disclosures:
        if keyword not in d.title:
            continue
        try:
            d_dt = datetime.date(*(int(p) for p in d.disclosure_date.split("/")))
        except ValueError:
            continue
        diff = abs((d_dt - event_dt).days)
        if diff > max_days_diff:
            continue
        if best_diff is None or diff < best_diff:
            best = d
            best_diff = diff
    return best


_PDF_MAGIC = b"%PDF"


class GetClientLike(Protocol):
    def get(self, url: str) -> bytes: ...


def download_pdf(client: GetClientLike, pdf_url: str) -> bytes:
    body = client.get(pdf_url)
    if body[:4] != _PDF_MAGIC:
        raise RuntimeError(f"{pdf_url}: PDFとして期待される形式ではありません")
    return body

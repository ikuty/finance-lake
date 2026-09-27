from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import jpx_disclosure_client  # noqa: E402

# 実機確認済み(2026-09-27)の東証上場会社情報サービスの実際のHTML構造を模したフィクスチャ
# （<tr id="1101_N">が[決算情報]カテゴリの行であることを実機確認済み）。
SAMPLE_HTML = """
<tr id="1101_0">
    <td align="center">2026/08/07</td>
    <td >
        <div class="txtLink2">
            <div class="txtLink2_InnerDiv">
                <a href="/disc/13010/140120260806512043.pdf" target="linkWin9_1">
                    2027年3月期　第1四半期決算短信[日本基準]（連結）
                </a>
            </div>
        </div>
    </td>
</tr>
<tr id="1101_1">
    <td align="center">2026/05/22</td>
    <td >
        <div class="txtLink2">
            <div class="txtLink2_InnerDiv">
                <a href="/disc/13010/140120260521543335.pdf" target="linkWin9_1">
                    2026年3月期決算IR説明会資料
                </a>
            </div>
        </div>
    </td>
</tr>
<tr id="1101_4">
    <td align="center">2026/02/06</td>
    <td >
        <div class="txtLink2">
            <div class="txtLink2_InnerDiv">
                <a href="/disc/13010/140120260205548832.pdf" target="linkWin9_1">
                    業績予想の修正に関するお知らせ
                </a>
            </div>
        </div>
    </td>
</tr>
<tr id="1102_0">
    <td align="center">2026/07/01</td>
    <td >
        <div class="txtLink2">
            <div class="txtLink2_InnerDiv">
                <a href="/disc/13010/999999999999999999.pdf" target="linkWin9_1">
                    決定事実カテゴリの何か（対象外のはず）
                </a>
            </div>
        </div>
    </td>
</tr>
"""


def test_parse_kessan_disclosures_extracts_only_1101_rows() -> None:
    disclosures = jpx_disclosure_client.parse_kessan_disclosures(SAMPLE_HTML)
    assert len(disclosures) == 3
    assert all("999999999999999999" not in d.pdf_url for d in disclosures)


def test_parse_kessan_disclosures_resolves_absolute_url() -> None:
    disclosures = jpx_disclosure_client.parse_kessan_disclosures(SAMPLE_HTML)
    assert disclosures[0].pdf_url == "https://www2.jpx.co.jp/disc/13010/140120260806512043.pdf"


def test_select_matching_disclosure_finds_exact_date_and_keyword() -> None:
    disclosures = jpx_disclosure_client.parse_kessan_disclosures(SAMPLE_HTML)
    match = jpx_disclosure_client.select_matching_disclosure(disclosures, "2026-08-07", "kessan_tanshin")
    assert match is not None
    assert match.pdf_url.endswith("140120260806512043.pdf")


def test_select_matching_disclosure_finds_forecast_revision() -> None:
    disclosures = jpx_disclosure_client.parse_kessan_disclosures(SAMPLE_HTML)
    match = jpx_disclosure_client.select_matching_disclosure(disclosures, "2026-02-06", "forecast_revision")
    assert match is not None
    assert match.pdf_url.endswith("140120260205548832.pdf")


def test_select_matching_disclosure_returns_none_when_no_keyword_match() -> None:
    disclosures = jpx_disclosure_client.parse_kessan_disclosures(SAMPLE_HTML)
    match = jpx_disclosure_client.select_matching_disclosure(disclosures, "2026-05-22", "forecast_revision")
    assert match is None


def test_title_matches_kind_catches_non_contiguous_forecast_revision_title() -> None:
    # tdnet_clientと同じ理由（実機確認済み、2026-09-27）で緩い判定にしている。
    assert jpx_disclosure_client._title_matches_kind(
        "業績予想(連結)の修正に関するお知らせ", "forecast_revision"
    ) is True


def test_select_matching_disclosure_respects_max_days_diff() -> None:
    disclosures = jpx_disclosure_client.parse_kessan_disclosures(SAMPLE_HTML)
    match = jpx_disclosure_client.select_matching_disclosure(
        disclosures, "2026-01-01", "forecast_revision", max_days_diff=3
    )
    assert match is None


# 実機確認済み(2026-09-27)のバグ再現用フィクスチャ: 北海電力(95090)が同日同時刻に
# 「業績予想(連結)の修正に関するお知らせ」と「2026年度 連結業績予想の修正について」
# という2件の別文書を提出しており、JPX側にも対応する2行が別々に存在する。
# 日付近似のみで選ぶ旧ロジックでは両方が同じ1行（診断時は診断で先に見つかった方）に
# 誤って紐付いてしまっていた。
_HOKKAIDO_POWER_DISCLOSURES = [
    jpx_disclosure_client.JpxDisclosure(
        disclosure_date="2026/09/25",
        title="2026年度 連結業績予想の修正について",
        pdf_url="https://www2.jpx.co.jp/disc/95090/140120260916537243.pdf",
    ),
    jpx_disclosure_client.JpxDisclosure(
        disclosure_date="2026/09/25",
        title="業績予想(連結)の修正に関するお知らせ",
        pdf_url="https://www2.jpx.co.jp/disc/95090/140120260916537214.pdf",
    ),
]


def test_select_matching_disclosure_prefers_exact_title_match_over_closest_date() -> None:
    match = jpx_disclosure_client.select_matching_disclosure(
        _HOKKAIDO_POWER_DISCLOSURES,
        "2026-09-25",
        "forecast_revision",
        event_title="業績予想(連結)の修正に関するお知らせ",
    )
    assert match is not None
    assert match.pdf_url.endswith("537214.pdf")


def test_select_matching_disclosure_prefers_exact_title_match_for_other_event_too() -> None:
    match = jpx_disclosure_client.select_matching_disclosure(
        _HOKKAIDO_POWER_DISCLOSURES,
        "2026-09-25",
        "forecast_revision",
        event_title="2026年度 連結業績予想の修正について",
    )
    assert match is not None
    assert match.pdf_url.endswith("537243.pdf")


def test_select_matching_disclosure_excludes_already_assigned_pdf_urls() -> None:
    # event_titleが無い（または一致しない）場合でも、既に他のイベントに割り当て
    # 済みのPDFは除外され、同じPDFが二重に選ばれないようにする安全弁。
    used = {"https://www2.jpx.co.jp/disc/95090/140120260916537243.pdf"}
    match = jpx_disclosure_client.select_matching_disclosure(
        _HOKKAIDO_POWER_DISCLOSURES,
        "2026-09-25",
        "forecast_revision",
        exclude_pdf_urls=used,
    )
    assert match is not None
    assert match.pdf_url.endswith("537214.pdf")


class _FakeClient:
    def __init__(self, page_body: bytes) -> None:
        self._page_body = page_body
        self.get_calls: list[str] = []
        self.post_calls: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str) -> bytes:
        self.get_calls.append(url)
        return b""

    def post_form(self, url: str, fields: dict[str, str], referer: str | None = None) -> bytes:
        self.post_calls.append((url, fields))
        return self._page_body


def test_fetch_company_page_sends_expected_form_sequence() -> None:
    client = _FakeClient(SAMPLE_HTML.encode("utf-8"))
    html = jpx_disclosure_client.fetch_company_page(client, "13010")

    assert html == SAMPLE_HTML
    assert client.get_calls == [jpx_disclosure_client.SEARCH_PAGE_URL]
    assert client.post_calls[0][0] == jpx_disclosure_client.SEARCH_POST_URL
    assert client.post_calls[0][1]["eqMgrCd"] == "13010"
    assert client.post_calls[1][0] == jpx_disclosure_client.DETAIL_POST_URL
    assert client.post_calls[1][1]["mgrCd"] == "13010"


def test_download_pdf_validates_magic_bytes() -> None:
    class _PdfClient:
        def get(self, url: str) -> bytes:
            return b"%PDF-1.6 fake content"

    body = jpx_disclosure_client.download_pdf(_PdfClient(), "https://example.com/a.pdf")
    assert body.startswith(b"%PDF")


def test_download_pdf_rejects_non_pdf_response() -> None:
    class _HtmlClient:
        def get(self, url: str) -> bytes:
            return b"<html>not a pdf</html>"

    with pytest.raises(RuntimeError, match="PDF"):
        jpx_disclosure_client.download_pdf(_HtmlClient(), "https://example.com/a.pdf")

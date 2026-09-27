from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import db  # noqa: E402
import tdnet_client  # noqa: E402

# 実機確認済み(2026-09-27)のTDnet日次一覧の実際のHTML構造を模したフィクスチャ。
SAMPLE_HTML = """
<table id="main-list-table">
<tr>
<td class="oddnew-L kjTime" noWrap>15:30</td>
<td class="oddnew-M kjCode" noWrap>13010</td>
<td class="oddnew-M kjName" noWrap>極洋 </td>
<td class="oddnew-M kjTitle" align="left"><a href="140120260807000001.pdf" target="_blank">2027年3月期　第1四半期決算短信[日本基準]（連結）</a></td>
<td class="oddnew-M kjXbrl" noWrap align="center"> </td>
</tr>
<tr>
<td class="evennew-L kjTime" noWrap>15:00</td>
<td class="evennew-M kjCode" noWrap>94700</td>
<td class="evennew-M kjName" noWrap>学研ＨＤ </td>
<td class="evennew-M kjTitle" align="left"><a href="140120260807000002.pdf" target="_blank">代表取締役の異動に関するお知らせ</a></td>
</tr>
<tr>
<td class="oddnew-L kjTime" noWrap>14:00</td>
<td class="oddnew-M kjCode" noWrap>67020</td>
<td class="oddnew-M kjName" noWrap>富士通 </td>
<td class="oddnew-M kjTitle" align="left"><a href="140120260807000003.pdf" target="_blank">業績予想の修正に関するお知らせ</a></td>
</tr>
</table>
"""


def test_parse_list_page_extracts_all_rows() -> None:
    rows = tdnet_client.parse_list_page(SAMPLE_HTML.encode("utf-8"))
    assert len(rows) == 3
    assert rows[0].kj_time == "15:30"
    assert rows[0].tdnet_code == "13010"
    assert rows[0].company_name == "極洋"
    assert "決算短信" in rows[0].title


def test_filter_and_match_selects_only_relevant_titles_and_known_companies(tmp_path: Path) -> None:
    conn = db.init_db(tmp_path / "test.db")
    db.upsert_company(conn, "E00012", "13010", "1301", "極洋")
    db.upsert_company(conn, "E01766", "67020", "6702", "富士通")
    conn.commit()

    rows = tdnet_client.parse_list_page(SAMPLE_HTML.encode("utf-8"))
    matched = tdnet_client.filter_and_match(rows, conn)

    assert {m.tdnet_code for m in matched} == {"13010", "67020"}  # 学研HDの人事異動は対象外
    kinds = {m.tdnet_code: m.disclosure_kind for m in matched}
    assert kinds["13010"] == "kessan_tanshin"
    assert kinds["67020"] == "forecast_revision"


def test_filter_and_match_excludes_unknown_company(tmp_path: Path) -> None:
    conn = db.init_db(tmp_path / "test.db")
    # 極洋(13010)を登録しない = seedに無い銘柄として扱う
    rows = tdnet_client.parse_list_page(SAMPLE_HTML.encode("utf-8"))
    matched = tdnet_client.filter_and_match(rows, conn)
    assert matched == []


class _FakeClient:
    def __init__(self, pages: dict[int, bytes]) -> None:
        self._pages = pages
        self.requested_urls: list[str] = []

    def get(self, url: str) -> bytes:
        self.requested_urls.append(url)
        for page_num, body in self._pages.items():
            if f"_{page_num:03d}_" in url:
                return body
        raise RuntimeError(f"{url}: HTTPエラー status=404")


def test_fetch_list_page_uses_compact_date_without_hyphens() -> None:
    # TDnetのURLはYYYYMMDD形式(ハイフン無し)を要求する。呼び出し側はISO形式
    # (YYYY-MM-DD)で日付を渡すため、ここで変換されることを確認する
    # （2026-09-27実機検証で発覚: 変換していないと1ページ目から404になり、
    # 検知件数が常に0件になるバグがあった）。
    client = _FakeClient({1: SAMPLE_HTML.encode("utf-8")})
    tdnet_client.fetch_list_page(client, "2026-08-07", 1)
    assert client.requested_urls == ["https://www.release.tdnet.info/inbs/I_list_001_20260807.html"]


def test_fetch_list_page_returns_none_on_404() -> None:
    client = _FakeClient({1: SAMPLE_HTML.encode("utf-8")})
    assert tdnet_client.fetch_list_page(client, "20260807", 2) is None


def test_run_for_date_paginates_and_stores_events(tmp_path: Path) -> None:
    conn = db.init_db(tmp_path / "test.db")
    db.upsert_company(conn, "E00012", "13010", "1301", "極洋")
    db.upsert_company(conn, "E01766", "67020", "6702", "富士通")
    conn.commit()

    client = _FakeClient({1: SAMPLE_HTML.encode("utf-8")})
    n = tdnet_client.run_for_date(conn, client, "2026-08-07")

    assert n == 2
    assert db.already_done_tdnet(conn, "2026-08-07") is True
    assert len(db.pending_tdnet_events(conn)) == 2


def test_run_for_date_is_idempotent_on_rerun(tmp_path: Path) -> None:
    conn = db.init_db(tmp_path / "test.db")
    db.upsert_company(conn, "E00012", "13010", "1301", "極洋")
    conn.commit()

    client = _FakeClient({1: SAMPLE_HTML.encode("utf-8")})
    tdnet_client.run_for_date(conn, client, "2026-08-07")
    n_second = tdnet_client.run_for_date(conn, client, "2026-08-07")

    assert n_second == 0  # 既に保存済みのため新規挿入は0件

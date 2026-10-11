from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import backfill_reit  # noqa: E402


def _write_list_response(data_dir: Path, file_date: str, results: list[dict[str, object]]) -> None:
    year, month, day = file_date.split("-")
    path = data_dir / "response" / year / month / day / "document_list.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"results": results}), encoding="utf-8")


def test_find_reit_docs_filters_by_form_code(tmp_path: Path) -> None:
    _write_list_response(
        tmp_path,
        "2026-04-24",
        [
            {"docID": "S100REIT", "edinetCode": "E24347", "formCode": "07B000", "secCode": None},
            {"docID": "S100FUND", "edinetCode": "E12444", "formCode": "07A000", "secCode": None},
            {"docID": "S100CORP", "edinetCode": "E00012", "formCode": None, "secCode": "13010"},
        ],
    )

    docs = backfill_reit.find_reit_docs(tmp_path, None)

    assert len(docs) == 1
    file_date, doc = docs[0]
    assert file_date == "2026-04-24"
    assert doc["docID"] == "S100REIT"


def test_find_reit_docs_scans_multiple_dates(tmp_path: Path) -> None:
    _write_list_response(
        tmp_path, "2026-04-24", [{"docID": "S100AAAA", "edinetCode": "E24347", "formCode": "07B000"}]
    )
    _write_list_response(
        tmp_path, "2026-09-17", [{"docID": "S100BBBB", "edinetCode": "E13206", "formCode": "10B000"}]
    )

    docs = backfill_reit.find_reit_docs(tmp_path, None)

    assert {d["docID"] for _, d in docs} == {"S100AAAA", "S100BBBB"}


def test_find_reit_docs_filters_by_edinet_code(tmp_path: Path) -> None:
    _write_list_response(
        tmp_path,
        "2026-04-24",
        [
            {"docID": "S100AAAA", "edinetCode": "E24347", "formCode": "07B000"},
            {"docID": "S100BBBB", "edinetCode": "E13206", "formCode": "07B000"},
        ],
    )

    docs = backfill_reit.find_reit_docs(tmp_path, "E13206")

    assert len(docs) == 1
    assert docs[0][1]["docID"] == "S100BBBB"

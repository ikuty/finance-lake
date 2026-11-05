from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import promote_staging as ps  # noqa: E402


def _mk(root: Path, date: str, code: str) -> None:
    y, m, d = date.split("-")
    f = root / y / m / d / code / "a.pdf"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"x")


def test_promote_moves_only_requested_month_and_is_idempotent(tmp_path: Path) -> None:
    st, raw = tmp_path / "st", tmp_path / "raw"
    _mk(st, "2026-05-10", "E1")
    _mk(st, "2026-05-10", "E2")
    _mk(st, "2026-06-01", "E3")
    s = ps.promote_month(st, raw, "2026-05", dry_run=False)
    assert s.promoted == 2
    assert (raw / "2026/05/10/E1/a.pdf").exists()
    assert not (st / "2026/05").exists()
    assert (st / "2026/06/01/E3/a.pdf").exists()  # 他の月は残る
    assert ps.promote_month(st, raw, "2026-05", dry_run=False).promoted == 0


def test_existing_destination_is_not_overwritten(tmp_path: Path) -> None:
    st, raw = tmp_path / "st", tmp_path / "raw"
    _mk(st, "2026-05-10", "E1")
    _mk(raw, "2026-05-10", "E1")
    s = ps.promote_month(st, raw, "2026-05", dry_run=False)
    assert s.skipped_existing == 1 and s.promoted == 0
    assert (st / "2026/05/10/E1/a.pdf").exists()


def test_dry_run_moves_nothing(tmp_path: Path) -> None:
    st, raw = tmp_path / "st", tmp_path / "raw"
    _mk(st, "2026-05-10", "E1")
    assert ps.promote_month(st, raw, "2026-05", dry_run=True).promoted == 1
    assert (st / "2026/05/10/E1/a.pdf").exists() and not raw.exists()

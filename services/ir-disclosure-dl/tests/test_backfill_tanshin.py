from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import backfill_tanshin as bt  # noqa: E402


def test_consecutive_rate_limit_errors_abort() -> None:
    g = bt.AbortGuard(consecutive_limit=3)
    g.record_failure("x: HTTPエラー status=403", 1.0)
    g.record_failure("x: リトライ上限に達しました (status=429)", 1.0)
    with pytest.raises(bt.BackfillAborted):
        g.record_failure("x: HTTPエラー status=403", 1.0)


def test_success_resets_consecutive_counter() -> None:
    g = bt.AbortGuard(consecutive_limit=3)
    g.record_failure("status=429", 1.0)
    g.record_failure("status=429", 1.0)
    g.record_success(1.0)
    g.record_failure("status=429", 1.0)  # 中断しない


def test_non_rate_limit_error_does_not_trigger_consecutive_abort() -> None:
    g = bt.AbortGuard(consecutive_limit=2, min_attempts=1000)
    for _ in range(5):
        g.record_failure("ネットワークエラー", 1.0)


def test_slow_responses_abort() -> None:
    g = bt.AbortGuard(slow_window=3, slow_seconds=5.0)
    g.record_success(9.0)
    g.record_success(9.0)
    with pytest.raises(bt.BackfillAborted):
        g.record_success(9.0)


def test_failure_rate_abort() -> None:
    g = bt.AbortGuard(min_attempts=20, max_failure_rate=0.05)
    for _ in range(17):
        g.record_success(1.0)
    g.record_failure("other", 1.0)
    g.record_failure("other", 1.0)
    with pytest.raises(bt.BackfillAborted):
        g.record_failure("other", 1.0)  # 3/20 = 15%


def test_progress_roundtrip(tmp_path: Path) -> None:
    assert bt.load_progress(tmp_path) == {}
    bt.save_progress(tmp_path, {"E00001": {"found": 3, "downloaded": 3, "skipped": 0}}, "2024-03-01")
    assert bt.load_progress(tmp_path)["E00001"]["found"] == 3

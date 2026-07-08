"""Tests for the owner's freeform problem inbox (ProblemQueue)."""

from __future__ import annotations

from pathlib import Path

from vision.config import Settings
from vision.council.problems import ProblemQueue


def _queue(path: Path) -> ProblemQueue:
    return ProblemQueue(Settings(_env_file=None, COUNCIL_PROBLEM_QUEUE_PATH=str(path)))


def test_peek_and_consume_are_fifo_and_rewrite_the_file(tmp_path: Path) -> None:
    f = tmp_path / "problems.md"
    f.write_text(
        "Problem one.\nWe fixed it by X.\n\n---\n\nProblem two.\nWe fixed it by Y.\n",
        encoding="utf-8",
    )
    q = _queue(f)

    # peek does not consume; consume pops the head (FIFO).
    assert q.peek().startswith("Problem one")
    head = q.consume_head()
    assert head.startswith("Problem one") and "fixed it by X" in head
    # The file now holds only the second problem.
    assert "Problem one" not in f.read_text(encoding="utf-8")
    assert q.peek().startswith("Problem two")


def test_empty_or_missing_inbox_returns_none(tmp_path: Path) -> None:
    q = _queue(tmp_path / "does_not_exist.md")
    assert q.peek() is None
    assert q.consume_head() is None


def test_consuming_the_last_problem_empties_the_file(tmp_path: Path) -> None:
    f = tmp_path / "problems.md"
    f.write_text("The only problem.\n", encoding="utf-8")
    q = _queue(f)
    assert q.consume_head().startswith("The only problem")
    assert q.consume_head() is None
    assert f.read_text(encoding="utf-8").strip() == ""

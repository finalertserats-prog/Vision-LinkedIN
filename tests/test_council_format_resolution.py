"""Unit tests for FORMAT resolution — the fix for `format: "unknown"`.

WHY this suite exists: every council draft ever written recorded
``format = "unknown"`` because the composing voice reliably drops the ``FORMAT:``
header and just returns the post prose (the same unreliable-structured-output
failure the diagram lane already had to work around). A format that is never
recorded cannot be avoided next time, so the format-variety engine was blind and
the posts drifted into the same shape.

The fix does not hope harder for the header. It ASSIGNS a shape up front, tells
the composer to write in it, and resolves the recorded format as "whatever the
voice honestly echoed, else the shape we assigned" — true by construction either
way. These tests pin that contract with NO real model and a temp state file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vision.council.compose import Composer
from vision.council.formats import (
    FORMATS,
    UNCONDITIONAL_FORMATS,
    RecentFormatStore,
    choose_assigned_format,
)
from tests.test_council import FakeVoices, _delib, _settings

# A composition WITHOUT the FORMAT:/SITUATION: headers — exactly what the live
# composer returns in practice, and what used to parse as "unknown".
_HEADERLESS_COMPOSITION = (
    "POST:\n"
    + ("A plain reflection that runs long enough to clear the parse-miss guard. " * 6)
    + "\n\nCOUNCIL:\n- one\n- two\n- three\nPowered by Brahmastra\n"
)

# The same post, but the voice DID echo a valid (conditional) format.
_ECHOED_COMPOSITION = (
    "FORMAT: rare_consensus\n"
    "SITUATION: agreed - all three converged\n"
    "POST:\n"
    + ("A plain reflection that runs long enough to clear the parse-miss guard. " * 6)
    + "\n\nCOUNCIL:\n- one\n- two\n- three\nPowered by Brahmastra\n"
)


def test_assigned_format_is_unconditional_and_avoids_recent() -> None:
    """The up-front pick never assigns a shape that needs a specific outcome."""
    # Arrange: two unconditional shapes are recently used.
    recent = list(UNCONDITIONAL_FORMATS)[:2]

    # Act
    picks = {choose_assigned_format(recent) for _ in range(40)}

    # Assert: only unconditional shapes, and never a recent one. Assigning
    # 'rare_consensus' or 'one_changed_mind' up front would force a dishonest
    # framing, which is why the pool excludes them.
    assert picks <= UNCONDITIONAL_FORMATS
    assert not picks & set(recent)


def test_assigned_format_falls_back_when_every_shape_is_recent() -> None:
    """An exhausted pool reuses a shape rather than returning nothing."""
    # Arrange / Act
    pick = choose_assigned_format(list(UNCONDITIONAL_FORMATS))

    # Assert
    assert pick in UNCONDITIONAL_FORMATS


def test_headerless_reply_resolves_to_the_assigned_format(tmp_path: Path) -> None:
    """A dropped FORMAT: header no longer yields 'unknown'."""
    # Arrange: the voice returns a post with no headers at all.
    composer = Composer(
        voices=FakeVoices(lambda voice, prompt: _HEADERLESS_COMPOSITION),
        recent_store=RecentFormatStore(path=tmp_path / "s.json"),
        settings=_settings(tmp_path),
    )

    # Act
    result = composer.compose(_delib())

    # Assert: a real, known shape — the one we instructed the composer to write.
    assert result.format != "unknown"
    assert result.format in FORMATS


def test_echoed_format_wins_over_the_assigned_one(tmp_path: Path) -> None:
    """When the voice honestly reports a shape, that reporting is trusted."""
    # Arrange
    composer = Composer(
        voices=FakeVoices(lambda voice, prompt: _ECHOED_COMPOSITION),
        recent_store=RecentFormatStore(path=tmp_path / "s.json"),
        settings=_settings(tmp_path),
    )

    # Act
    result = composer.compose(_delib())

    # Assert: the conditional shape the voice actually used, not our assignment.
    assert result.format == "rare_consensus"


def test_resolved_format_is_recorded_for_the_next_run(tmp_path: Path) -> None:
    """The resolved shape lands in the recent store so variety actually works."""
    # Arrange
    store = RecentFormatStore(path=tmp_path / "s.json")
    composer = Composer(
        voices=FakeVoices(lambda voice, prompt: _HEADERLESS_COMPOSITION),
        recent_store=store,
        settings=_settings(tmp_path),
    )

    # Act
    result = composer.compose(_delib())

    # Assert: previously nothing was ever recorded, because 'unknown' is not a
    # FORMATS key — which is precisely why the variety window stayed empty.
    assert store.recent() == [result.format]


def test_assigned_format_appears_in_the_compose_prompt(tmp_path: Path) -> None:
    """The composer is actually TOLD which shape to write, not left to guess."""
    # Arrange: capture the prompt the composing voice receives.
    seen: list[str] = []

    def responder(voice: str, prompt: str) -> str:
        seen.append(prompt)
        return _HEADERLESS_COMPOSITION

    composer = Composer(
        voices=FakeVoices(responder),
        recent_store=RecentFormatStore(path=tmp_path / "s.json"),
        settings=_settings(tmp_path),
    )

    # Act
    result = composer.compose(_delib())

    # Assert: the assigned shape is named in the instruction the voice got.
    assert seen and result.format in seen[0]


@pytest.mark.parametrize("name", sorted(UNCONDITIONAL_FORMATS))
def test_every_unconditional_format_is_a_real_format(name: str) -> None:
    """The unconditional pool cannot drift out of sync with the menu."""
    # Arrange / Act / Assert
    assert name in FORMATS

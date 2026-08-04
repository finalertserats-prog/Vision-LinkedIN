"""Unit tests for the council's VARIETY rules (owner feedback 2026-08-04).

WHY this suite exists: the owner's complaint was that the feed had gone stale in
two specific ways — nearly every post carried a process-flow diagram (so the
hand-drawn art almost never ran, and when it did it always looked the same), and
the subjects circled the same territory post after post.

These tests pin the three rules that fix that, WITHOUT calling a real model or
writing outside a tmp path:

  * art registers rotate and never repeat back-to-back;
  * a diagram may not follow a diagram — art gets the next turn;
  * a proposed topic that echoes a recent THEME is skipped, not just an exact
    string repeat.

Each test follows AAA (Arrange → Act → Assert) with one behaviour per test.
"""

from __future__ import annotations

import json
from pathlib import Path

from vision.config import Settings
from vision.council.compose import DiagramSpec
from vision.council.topics import RecentTopicStore, _shares_theme
from vision.council.visual import (
    IMAGE_TYPE_CONCEPT,
    IMAGE_TYPE_DIAGRAM,
    _ART_STYLES,
    _CouncilImageLedger,
    _pick_art_style,
    decide_council_image,
)


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    """Pinned settings whose state files live under a tmp dir (never the repo)."""
    base: dict[str, object] = {
        "SECRET_HMAC_KEY": "variety-test-hmac",  # noqa: S106 - test placeholder
        "IMAGE_ENABLED": True,
        "COUNCIL_IMAGE_ENABLED": True,
        "COUNCIL_DIAGRAM_ENABLED": True,
        # Every eligible post gets art, so a test never trips the rotation skip.
        "COUNCIL_IMAGE_EVERY_N": 1,
        "COUNCIL_IMAGE_STATE_PATH": str(tmp_path / "image_state.json"),
        "COUNCIL_TOPIC_STATE_PATH": str(tmp_path / "topic_state.json"),
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_art_style_never_repeats_the_previous_post() -> None:
    """The picker excludes the last-used register so two posts never match."""
    # Arrange / Act: ask many times with the same "last" style.
    picks = {_pick_art_style("manga_ink") for _ in range(40)}

    # Assert: the excluded style never comes back, and the others do appear.
    assert "manga_ink" not in picks
    assert picks <= set(_ART_STYLES) and picks


def test_art_style_falls_back_to_full_menu_when_last_is_unknown() -> None:
    """An unknown/None previous style still yields a valid register (never empty)."""
    # Arrange / Act
    pick = _pick_art_style(None)

    # Assert
    assert pick in _ART_STYLES


def test_concept_art_records_its_style_for_next_time(tmp_path: Path) -> None:
    """Choosing art stamps the register + kind so the next run can avoid them."""
    # Arrange
    settings = _settings(tmp_path)

    # Act
    choice = decide_council_image("A reflection with no structure to draw.", settings=settings)

    # Assert
    assert choice.image_type == IMAGE_TYPE_CONCEPT
    ledger = _CouncilImageLedger.from_settings(settings)
    assert ledger.last_art_style() in _ART_STYLES
    assert ledger.last_visual_kind() == IMAGE_TYPE_CONCEPT


def test_style_is_embedded_in_the_illustration_prompt(tmp_path: Path) -> None:
    """The chosen register leads the prompt so the image model actually honours it."""
    # Arrange
    settings = _settings(tmp_path)

    # Act
    choice = decide_council_image("A plain human reflection.", settings=settings)

    # Assert: the prompt opens with one of the three registers' wording.
    prompt = choice.illustration_prompt or ""
    assert any(prompt.startswith(text) for text in _ART_STYLES.values())


def test_diagram_is_used_when_the_previous_post_was_not_a_diagram(tmp_path: Path) -> None:
    """A worthwhile diagram still wins when it did not run last time."""
    # Arrange
    settings = _settings(tmp_path)
    spec = DiagramSpec(mermaid="flowchart TD\n A[One] --> B[Two]")

    # Act
    choice = decide_council_image("A technical post.", diagram=spec, settings=settings)

    # Assert
    assert choice.image_type == IMAGE_TYPE_DIAGRAM


def test_diagram_cannot_run_twice_in_a_row(tmp_path: Path) -> None:
    """After a diagram post, the next post degrades to art even if a diagram exists."""
    # Arrange: stamp the ledger as if the previous post was a diagram.
    settings = _settings(tmp_path)
    _CouncilImageLedger.from_settings(settings).remember_visual_kind(IMAGE_TYPE_DIAGRAM)
    spec = DiagramSpec(mermaid="flowchart TD\n A[One] --> B[Two]")

    # Act
    choice = decide_council_image("Another technical post.", diagram=spec, settings=settings)

    # Assert: art gets the turn, which is the whole point of the rule.
    assert choice.image_type == IMAGE_TYPE_CONCEPT


def test_shares_theme_catches_a_paraphrased_repeat() -> None:
    """A topic reusing the same subject words counts as the same theme."""
    # Arrange
    recent = ["Hospitals still run Windows 7 on infusion pumps nobody can patch"]

    # Act / Assert: same subject, different wording → blocked.
    assert _shares_theme("Why hospitals cannot patch their infusion pumps", recent)


def test_shares_theme_allows_a_genuinely_different_subject() -> None:
    """An unrelated topic is not blocked by the theme filter."""
    # Arrange
    recent = ["Hospitals still run Windows 7 on infusion pumps nobody can patch"]

    # Act / Assert
    assert not _shares_theme("What makes a good code review feel worth reading", recent)


def test_recent_topic_store_round_trips_and_caps(tmp_path: Path) -> None:
    """Topics persist most-recent-first and are bounded by the window."""
    # Arrange
    store = RecentTopicStore(path=tmp_path / "topics.json", window=3)

    # Act
    for topic in ["first", "second", "third", "fourth"]:
        store.remember(topic)

    # Assert: newest first, oldest dropped at the window boundary.
    assert store.recent() == ["fourth", "third", "second"]


def test_recent_topic_store_survives_corrupt_state(tmp_path: Path) -> None:
    """Corrupt state reads as no history rather than crashing the council."""
    # Arrange
    path = tmp_path / "topics.json"
    path.write_text("{not json", encoding="utf-8")
    store = RecentTopicStore(path=path, window=4)

    # Act / Assert
    assert store.recent() == []


def test_recent_topic_store_ignores_wrong_shape(tmp_path: Path) -> None:
    """A JSON object (not a list) on disk is ignored, not trusted."""
    # Arrange
    path = tmp_path / "topics.json"
    path.write_text(json.dumps({"topics": ["x"]}), encoding="utf-8")
    store = RecentTopicStore(path=path, window=4)

    # Act / Assert
    assert store.recent() == []

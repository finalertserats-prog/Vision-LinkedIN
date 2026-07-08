"""Unit tests for the council IMAGE LANE (``vision.council.visual``).

WHY these tests (BRD §13.6 / §18 / §22): the council image lane shells out to the
deterministic card renderer AND (for concept illustrations) to agy via
``BrahmastraImageClient`` — a real subprocess + real AI. So EVERY test here MOCKS
``render_quote_card`` and the image client; NO real agy run, NO network, NO
LinkedIn call ever happens in a unit test (§22). We assert the contract the task
enumerates:

  1. the decision path — quote_card for a punchy one-liner, concept_illustration
     for an atmospheric post, none otherwise — sets the RIGHT image_* fields;
  2. a generation FAILURE degrades to image_type 'none' (never blocks the post);
  3. the weekly cap (``IMAGE_MAX_PER_WEEK``) is respected;
  4. the rotation heuristic (``COUNCIL_IMAGE_EVERY_N``) means NOT every post;
  5. the lane is a no-op when disabled.

Every test is AAA (Arrange → Act → Assert) with a single behavioural focus.
"""

from __future__ import annotations

from pathlib import Path

from vision.brahmastra.errors import ImageGenerationError
from vision.config import Settings
from vision.council.visual import (
    CouncilImageChoice,
    attach_council_image,
    decide_council_image,
)


# --- Fixtures / helpers -----------------------------------------------------


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    """Hermetic Settings with the image lane ON and all state under tmp_path.

    Pinning the state path + image dir under the test's tmp dir isolates the
    weekly-cap ledger and rendered PNGs from any real files.
    """
    base: dict[str, object] = {
        "COUNCIL_IMAGE_ENABLED": True,
        "COUNCIL_IMAGE_EVERY_N": 1,  # every eligible post, unless a test overrides
        "COUNCIL_IMAGE_DIR": str(tmp_path / "images"),
        "COUNCIL_IMAGE_STATE_PATH": str(tmp_path / ".council_image_state.json"),
        "IMAGE_MAX_PER_WEEK": 4,
        "IMAGE_ENABLED": True,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


# A post with a strong, quotable one-line punchline (short, declarative, no
# numbers) — the sensible default for a quote card.
_PUNCHY_POST = (
    "The tools we build quietly rebuild us.\n\n"
    "We keep debating whether the machine can think, and miss the quieter "
    "question of what thinking for us does to us over time."
)

# An atmospheric post with NO crisp one-liner — better suited to a concept
# illustration (text-free) than a quote card.
_ATMOSPHERIC_POST = (
    "There is a particular kind of morning in an old hospital corridor, when "
    "the light comes in low and the day has not yet decided what it will ask of "
    "anyone, and it is in that suspended hour that the whole weight of a system "
    "built by many hands over many years seems to hang in the ordinary air."
)


class _FakeImageClient:
    """A ``BrahmastraImageClient`` stand-in that never touches agy/subprocess.

    ``result`` is either PNG bytes to return or an ``ImageGenerationError`` to
    raise, so a test drives the success/failure path deterministically.
    """

    def __init__(self, result: bytes | ImageGenerationError) -> None:
        self._result = result
        self.calls: list[str] = []

    def illustrate(self, prompt: str, model: str | None = None) -> bytes:
        self.calls.append(prompt)
        if isinstance(self._result, ImageGenerationError):
            raise self._result
        return self._result


_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-card-bytes"


# --- 1. Decision: punchy post → quote_card ---------------------------------


def test_decide_returns_quote_card_for_punchy_one_liner(tmp_path: Path) -> None:
    # Arrange.
    settings = _settings(tmp_path)

    # Act.
    choice = decide_council_image(_PUNCHY_POST, settings=settings)

    # Assert: a punchy first line earns a quote card, carrying that exact line.
    assert choice.image_type == "quote_card"
    assert choice.quote_line == "The tools we build quietly rebuild us."


# --- 2. Decision: atmospheric post → concept_illustration ------------------


def test_decide_returns_concept_illustration_for_atmospheric_post(
    tmp_path: Path,
) -> None:
    # Arrange.
    settings = _settings(tmp_path)

    # Act.
    choice = decide_council_image(_ATMOSPHERIC_POST, settings=settings)

    # Assert: no crisp punchline → an atmospheric, text-free illustration, with a
    # prompt that MUST demand no text/words/letters (precision rule §13.6/D10).
    assert choice.image_type == "concept_illustration"
    assert choice.illustration_prompt
    assert "no text" in choice.illustration_prompt.lower()


# --- 3. Decision: disabled lane → none -------------------------------------


def test_decide_returns_none_when_council_image_disabled(tmp_path: Path) -> None:
    # Arrange: the council image lane is explicitly OFF.
    settings = _settings(tmp_path, COUNCIL_IMAGE_ENABLED=False)

    # Act.
    choice = decide_council_image(_PUNCHY_POST, settings=settings)

    # Assert: a disabled lane never proposes an image.
    assert choice.image_type == "none"


# --- 4. Rotation heuristic: NOT every post ---------------------------------


def test_rotation_skips_posts_between_every_n(tmp_path: Path) -> None:
    # Arrange: attach an image only every 3rd eligible post.
    settings = _settings(tmp_path, COUNCIL_IMAGE_EVERY_N=3)

    # Act: run the decision three times in a row (each advances the rotation).
    choices = [
        decide_council_image(_PUNCHY_POST, settings=settings) for _ in range(3)
    ]

    # Assert: exactly ONE of the three windows attaches an image; the others skip
    # to 'none' — the council is not image-heavy.
    attached = [c for c in choices if c.image_type != "none"]
    assert len(attached) == 1


# --- 5. Weekly cap respected ------------------------------------------------


def test_weekly_cap_blocks_further_images(tmp_path: Path) -> None:
    # Arrange: cap of 2 per week, every post eligible.
    settings = _settings(
        tmp_path, IMAGE_MAX_PER_WEEK=2, COUNCIL_IMAGE_EVERY_N=1
    )
    fake_render = lambda quote, **_: _PNG_BYTES  # noqa: E731 — tiny test stub

    # Act: attach an image four times; the ledger should stop after two.
    types: list[str] = []
    for _ in range(4):
        draft: dict[str, object] = {"post_text": _PUNCHY_POST}
        attach_council_image(
            draft,
            settings=settings,
            render_quote_card=fake_render,
            image_client=_FakeImageClient(_PNG_BYTES),
        )
        types.append(str(draft["image_type"]))

    # Assert: only the first TWO carried an image; the rest degraded to none.
    assert types.count("quote_card") == 2
    assert types.count("none") == 2


# --- 6. attach sets quote-card fields on the draft dict --------------------


def test_attach_quote_card_sets_image_fields(tmp_path: Path) -> None:
    # Arrange.
    settings = _settings(tmp_path)
    draft: dict[str, object] = {"post_text": _PUNCHY_POST}
    calls: list[str] = []

    def fake_render(quote: str, **_: object) -> bytes:
        calls.append(quote)
        return _PNG_BYTES

    # Act.
    attach_council_image(
        draft,
        settings=settings,
        render_quote_card=fake_render,
        image_client=_FakeImageClient(_PNG_BYTES),
    )

    # Assert: the quote card is rendered from the punchline and the draft's image_*
    # fields are set for the mailer/publisher — a real PNG written to disk.
    assert calls == ["The tools we build quietly rebuild us."]
    assert draft["image_type"] == "quote_card"
    assert draft["image_source"] == "deterministic"
    assert draft["image_prompt"] is None
    path = Path(str(draft["image_path"]))
    assert path.exists()
    assert path.read_bytes() == _PNG_BYTES


# --- 7. attach sets concept-illustration fields on the draft dict ----------


def test_attach_concept_illustration_sets_image_fields(tmp_path: Path) -> None:
    # Arrange.
    settings = _settings(tmp_path)
    draft: dict[str, object] = {"post_text": _ATMOSPHERIC_POST}
    client = _FakeImageClient(_PNG_BYTES)

    # Act: render_quote_card must NOT be called on this path.
    def fail_render(quote: str, **_: object) -> bytes:
        raise AssertionError("quote card must not render for an atmospheric post")

    attach_council_image(
        draft,
        settings=settings,
        render_quote_card=fail_render,
        image_client=client,
    )

    # Assert: the illustration path ran, wrote a PNG, and stamped the model source
    # + the text-free prompt on the draft.
    assert client.calls, "the image client should have been asked to illustrate"
    assert draft["image_type"] == "concept_illustration"
    assert draft["image_source"] == settings.image_model
    assert draft["image_prompt"]
    path = Path(str(draft["image_path"]))
    assert path.exists()


# --- 8. Generation failure degrades to none --------------------------------


def test_illustration_failure_degrades_to_none(tmp_path: Path) -> None:
    # Arrange: the image client raises — a real agy hiccup.
    settings = _settings(tmp_path)
    draft: dict[str, object] = {"post_text": _ATMOSPHERIC_POST}
    client = _FakeImageClient(ImageGenerationError("agy timed out"))

    # Act.
    attach_council_image(
        draft,
        settings=settings,
        render_quote_card=lambda quote, **_: _PNG_BYTES,
        image_client=client,
    )

    # Assert: a failed illustration NEVER blocks the post — the draft is text-only.
    assert draft["image_type"] == "none"
    assert draft["image_path"] is None


# --- 9. Quote-card render failure degrades to none -------------------------


def test_quote_card_render_failure_degrades_to_none(tmp_path: Path) -> None:
    # Arrange: the deterministic renderer raises (e.g. a layout ValueError).
    settings = _settings(tmp_path)
    draft: dict[str, object] = {"post_text": _PUNCHY_POST}

    def boom_render(quote: str, **_: object) -> bytes:
        raise ValueError("card layout overflowed")

    # Act.
    attach_council_image(
        draft,
        settings=settings,
        render_quote_card=boom_render,
        image_client=_FakeImageClient(_PNG_BYTES),
    )

    # Assert: a render blow-up degrades to text-only, never propagates.
    assert draft["image_type"] == "none"
    assert draft["image_path"] is None


# --- 10. Weekly-cap counts BOTH image types --------------------------------


def test_weekly_cap_counts_are_shared_across_types(tmp_path: Path) -> None:
    # Arrange: one image left this week; the choice is a quote card.
    settings = _settings(
        tmp_path, IMAGE_MAX_PER_WEEK=1, COUNCIL_IMAGE_EVERY_N=1
    )
    draft1: dict[str, object] = {"post_text": _PUNCHY_POST}
    draft2: dict[str, object] = {"post_text": _ATMOSPHERIC_POST}

    # Act: the first consumes the single weekly slot; the second must be capped
    # even though it is a different image TYPE.
    attach_council_image(
        draft1,
        settings=settings,
        render_quote_card=lambda quote, **_: _PNG_BYTES,
        image_client=_FakeImageClient(_PNG_BYTES),
    )
    attach_council_image(
        draft2,
        settings=settings,
        render_quote_card=lambda quote, **_: _PNG_BYTES,
        image_client=_FakeImageClient(_PNG_BYTES),
    )

    # Assert: the cap is shared — the second draft is text-only.
    assert draft1["image_type"] == "quote_card"
    assert draft2["image_type"] == "none"


# --- 11. CouncilImageChoice is an inert value object -----------------------


def test_choice_none_is_the_default_shape() -> None:
    # Arrange / Act: the explicit 'skip' sentinel.
    choice = CouncilImageChoice.none()

    # Assert: a none choice carries no line/prompt — a clean text-only signal.
    assert choice.image_type == "none"
    assert choice.quote_line is None
    assert choice.illustration_prompt is None


# --- 12. A disabled global IMAGE_ENABLED also skips -------------------------


def test_global_image_disabled_skips(tmp_path: Path) -> None:
    # Arrange: the council image lane is on, but the GLOBAL image switch is off.
    settings = _settings(tmp_path, IMAGE_ENABLED=False)

    # Act.
    choice = decide_council_image(_PUNCHY_POST, settings=settings)

    # Assert: the global kill-switch wins — no image.
    assert choice.image_type == "none"

"""Unit tests for the VISUALS lane (BRD §13.6, D8/D10 precision-first).

WHY these tests (BRD §18/§22 — tests are part of "done"): the visuals lane makes
two promises the whole precision-first design rests on —

  1. Deterministic renders are pixel-exact and carry only grounded numbers.
  2. The decision gate never hands numeric/word content to a diffusion model and
     defaults safely to ``none``.

Every test is AAA (Arrange → Act → Assert) with a single behavioural focus, and
the ONLY external dependency (``BrahmastraImageClient``) is mocked so no real
model is ever invoked.
"""

from __future__ import annotations

import io

from unittest.mock import MagicMock

import pytest
from PIL import Image

from vision.brahmastra.errors import ImageGenerationError
from vision.config import Settings, SignatureMode
from vision.visuals.card_renderer import (
    LINKEDIN_LANDSCAPE,
    LINKEDIN_SQUARE,
    render_chart,
    render_stat_card,
)
from vision.visuals.decide import (
    CardSpec,
    Datapoint,
    ImageDecision,
    ImageType,
    image_decision,
)
from vision.visuals.illustrate import generate_illustration

# --- Helpers ----------------------------------------------------------------


def _settings(**overrides: object) -> Settings:
    """Build a Settings object with a pinned palette for deterministic renders.

    WHY explicit construction: tests must control palette + signature mode
    without depending on the developer's environment or a real ``.env``.
    """
    base: dict[str, object] = {
        "CARD_BRAND_PALETTE": "navy=#0B1F3A;gold=#C9A24B",
        "POST_SIGNATURE_MODE": SignatureMode.OFF,
    }
    base.update(overrides)
    return Settings(**base)


def _grounded_spec() -> CardSpec:
    """A minimal, fully-grounded card spec (every datapoint has a source)."""
    return CardSpec(
        title="FDA AI Clearances",
        datapoints=[
            Datapoint(label="Devices cleared 2024", value="223", source_item_id="item-1"),
            Datapoint(label="YoY growth", value="14%", source_item_id="item-2"),
        ],
    )


# --- render_stat_card -------------------------------------------------------


def test_render_stat_card_returns_png_of_exact_landscape_dimensions() -> None:
    # Arrange
    spec = _grounded_spec()

    # Act
    data = render_stat_card(spec, _settings())

    # Assert — valid PNG opened back to the exact LinkedIn landscape size.
    with Image.open(io.BytesIO(data)) as image:
        assert image.format == "PNG"
        assert image.size == LINKEDIN_LANDSCAPE


def test_render_stat_card_missing_source_item_id_raises() -> None:
    # Arrange — one datapoint deliberately lacks provenance.
    spec = CardSpec(
        title="Ungrounded",
        datapoints=[Datapoint(label="Mystery", value="99")],
    )

    # Act / Assert — the renderer must fail loudly, never emit an ungrounded number.
    with pytest.raises(ValueError, match="source_item_id"):
        render_stat_card(spec, _settings())


def test_render_stat_card_applies_watermark_when_config_enables_it() -> None:
    # Arrange — identical spec, watermark OFF vs. CARD_WATERMARK.
    spec = _grounded_spec()

    # Act
    without = render_stat_card(spec, _settings(POST_SIGNATURE_MODE=SignatureMode.OFF))
    with_mark = render_stat_card(
        spec, _settings(POST_SIGNATURE_MODE=SignatureMode.CARD_WATERMARK)
    )

    # Assert — the watermark changes the pixels, so the byte streams differ,
    # while both remain valid exact-size PNGs.
    assert without != with_mark
    for data in (without, with_mark):
        with Image.open(io.BytesIO(data)) as image:
            assert image.size == LINKEDIN_LANDSCAPE


def test_render_stat_card_rejects_more_than_max_datapoints() -> None:
    # Arrange — four grounded datapoints exceed the legible-layout cap; the
    # third already lands on the source/watermark footer (~614px on 627px), so
    # the renderer must fail loudly rather than silently emit an overlapping card.
    spec = CardSpec(
        title="Too many numbers",
        datapoints=[
            Datapoint(label=f"Metric {n}", value=f"{n}0", source_item_id=f"item-{n}")
            for n in range(4)
        ],
    )

    # Act / Assert — a guard raises instead of drawing off-canvas.
    with pytest.raises(ValueError, match="datapoint"):
        render_stat_card(spec, _settings())


def test_render_stat_card_long_content_stays_within_canvas() -> None:
    # Arrange — pathologically long title/labels/values/source with the full
    # (max) datapoint count: the pre-fix fixed +150 stepping runs the last block
    # onto the footer and the long strings run off the right edge.
    from vision.visuals.card_renderer import stat_card_layout

    long = "Autonomous diagnostic imaging clearances across every FDA review pathway"
    spec = CardSpec(
        title=long,
        source_label="Source: " + long,
        datapoints=[
            Datapoint(
                label=f"{long} #{n}",
                value=f"{n}23,456,789 procedures",
                source_item_id=f"item-{n}",
            )
            for n in range(3)
        ],
    )

    # Act
    layout = stat_card_layout(spec, _settings())
    data = render_stat_card(spec, _settings())

    # Assert — computed content never exceeds the canvas, and the render still
    # produces a valid exact-size PNG.
    assert layout.content_bottom <= LINKEDIN_LANDSCAPE[1]
    with Image.open(io.BytesIO(data)) as image:
        assert image.size == LINKEDIN_LANDSCAPE


def test_render_stat_card_invalid_palette_color_degrades_to_default() -> None:
    # Arrange — a malformed configured colour must not reach Image.new; the
    # docstring promises a bad palette "degrades to on-brand defaults, never a
    # crash".
    spec = _grounded_spec()

    # Act — invalid navy value; renderer should fall back silently.
    data = render_stat_card(
        spec, _settings(CARD_BRAND_PALETTE="navy=notacolor;gold=#C9A24B")
    )

    # Assert — no exception, still an exact-size PNG.
    with Image.open(io.BytesIO(data)) as image:
        assert image.format == "PNG"
        assert image.size == LINKEDIN_LANDSCAPE


# --- render_chart -----------------------------------------------------------


def test_render_chart_returns_png_of_exact_square_dimensions() -> None:
    # Arrange
    spec = CardSpec(
        title="Weekly Funding ($M)",
        chart_type="bar",
        datapoints=[
            Datapoint(label="Mon", value="12", source_item_id="i-1"),
            Datapoint(label="Tue", value="18", source_item_id="i-2"),
            Datapoint(label="Wed", value="7", source_item_id="i-3"),
        ],
    )

    # Act
    data = render_chart(spec, _settings())

    # Assert
    with Image.open(io.BytesIO(data)) as image:
        assert image.format == "PNG"
        assert image.size == LINKEDIN_SQUARE


def test_render_chart_non_numeric_value_raises() -> None:
    # Arrange — a value that cannot be parsed as a number.
    spec = CardSpec(
        title="Broken",
        chart_type="bar",
        datapoints=[Datapoint(label="X", value="not-a-number", source_item_id="i-1")],
    )

    # Act / Assert
    with pytest.raises(ValueError, match="not numeric"):
        render_chart(spec, _settings())


# --- image_decision (the precision-first gate) ------------------------------


def test_image_decision_defaults_to_none_when_no_decision() -> None:
    # Arrange / Act
    result = image_decision("A post with no visual.", claims=[], decision=None)

    # Assert
    assert result is ImageType.NONE


def test_image_decision_numbers_force_informative_card_never_diffusion() -> None:
    # Arrange — synthesis proposed a diffusion illustration, but the copy has a
    # concrete number AND grounded datapoints exist.
    decision = ImageDecision(
        image_type=ImageType.CONCEPT_ILLUSTRATION,
        illustration_prompt="abstract neural network, muted palette",
        card_spec=_grounded_spec(),
    )

    # Act
    result = image_decision(
        "AI device clearances rose 14% this year.",
        claims=["223 devices cleared in 2024"],
        decision=decision,
    )

    # Assert — precision-first override: deterministic card, never diffusion.
    assert result is ImageType.INFORMATIVE_CARD


def test_image_decision_conceptual_prompt_allows_illustration() -> None:
    # Arrange — purely conceptual copy, no numbers, no datapoints.
    decision = ImageDecision(
        image_type=ImageType.CONCEPT_ILLUSTRATION,
        illustration_prompt="abstract calm horizon, muted palette",
    )

    # Act
    result = image_decision(
        "A reflection on the shape of trust in medicine.",
        claims=["trust is earned slowly"],
        decision=decision,
    )

    # Assert
    assert result is ImageType.CONCEPT_ILLUSTRATION


def test_image_decision_informative_card_without_datapoints_degrades_to_none() -> None:
    # Arrange — card requested but nothing grounded to render.
    decision = ImageDecision(image_type=ImageType.INFORMATIVE_CARD, card_spec=CardSpec(title="x"))

    # Act
    result = image_decision("No data here.", claims=[], decision=decision)

    # Assert
    assert result is ImageType.NONE


def test_image_decision_accepts_raw_dict_and_validates() -> None:
    # Arrange — the synthesis pass output as a raw JSON-shaped dict.
    payload = {
        "image_type": "informative-card",
        "rationale": "stat-centric",
        "card_spec": {
            "title": "Approvals",
            "datapoints": [{"label": "2024", "value": "223", "source_item_id": "i-1"}],
        },
        "illustration_prompt": None,
    }

    # Act
    result = image_decision("223 approvals in 2024.", claims=[], decision=payload)

    # Assert
    assert result is ImageType.INFORMATIVE_CARD


def test_image_decision_malformed_dict_degrades_to_none() -> None:
    # Arrange — an out-of-contract image_type is drift; the gate must not crash.
    payload = {"image_type": "spaceship"}

    # Act
    result = image_decision("copy", claims=[], decision=payload)

    # Assert
    assert result is ImageType.NONE


# --- generate_illustration (mocked BrahmastraImageClient) -------------------


def test_generate_illustration_returns_bytes_on_success() -> None:
    # Arrange — a mocked client that yields fake image bytes.
    client = MagicMock()
    client.illustrate.return_value = b"\x89PNG\r\n\x1a\nfake"

    # Act
    result = generate_illustration(
        "abstract horizon", client=client, settings=_settings()
    )

    # Assert — bytes flow through and the client was actually consulted.
    assert result == b"\x89PNG\r\n\x1a\nfake"
    client.illustrate.assert_called_once()


def test_generate_illustration_degrades_to_none_on_failure() -> None:
    # Arrange — the client raises the one degrade-gracefully error class.
    client = MagicMock()
    client.illustrate.side_effect = ImageGenerationError("model down")

    # Act
    result = generate_illustration("abstract horizon", client=client, settings=_settings())

    # Assert — failure becomes None (text-only fallback), never an exception.
    assert result is None


def test_generate_illustration_skips_empty_prompt() -> None:
    # Arrange — an empty prompt has nothing to generate.
    client = MagicMock()

    # Act
    result = generate_illustration("   ", client=client, settings=_settings())

    # Assert — skipped without ever calling the model.
    assert result is None
    client.illustrate.assert_not_called()

"""Unit tests for the TECH-POST DIAGRAM LANE (owner req 2026-07-11).

WHY these tests: a genuinely technical council post can carry a small mermaid
diagram that AMPLIFIES its idea, rendered DETERMINISTICALLY by the mermaid CLI
(``mmdc``) so text labels are safe (precision rule §13.6/D10). These tests MOCK
the subprocess / renderer so NO ``mmdc`` run, NO headless browser, NO network
ever happens. We assert the contract:

  1. compose PARSES a DIAGRAM section into a DiagramSpec, drops non-mermaid noise,
     strips a stray code fence, and DROPS a diagram that leaks an AI/model name;
  2. the decision path returns a diagram choice when a spec is present + the lane
     is enabled, BYPASSING the decorative every-N rotation, and falls back when
     the lane is disabled;
  3. the renderer shells out correctly on success and raises DiagramRenderError on
     every failure class (so the lane degrades to text-only, never blocks a post);
  4. a render FAILURE degrades the draft to image_type 'none'.

Every test is AAA (Arrange → Act → Assert) with a single behavioural focus.
"""

from __future__ import annotations

import subprocess
import types
from pathlib import Path

import pytest

from vision.config import Settings
from vision.council.compose import (
    DiagramSpec,
    _parse_composition,
    _parse_diagram,
)
from vision.council.visual import (
    IMAGE_TYPE_CONCEPT,
    IMAGE_TYPE_DIAGRAM,
    IMAGE_TYPE_NONE,
    attach_council_image,
    decide_council_image,
)
from vision.visuals import diagram_renderer
from vision.visuals.diagram_renderer import DiagramRenderError, render_mermaid


_MERMAID = "flowchart TD\n  A[Query] --> B{Grounded?}\n  B -->|no| C[Route to human]"

# A composition whose POST clears the 200-char floor, plus a DIAGRAM section.
_RAW_WITH_DIAGRAM = (
    "FORMAT: quiet_observation\n"
    "SITUATION: agreed - both saw the same failure mode\n"
    "POST:\n"
    "Consumer AI is built to always answer. A clinical system has to know when "
    "not to, and that single inversion changes the whole architecture. The path "
    "that matters most is the one where it abstains and routes to a human.\n"
    "COUNCIL:\n"
    "- always answering is a hazard near a patient\n"
    "- grounding beats recall\n"
    "- the abstain path is the product\n"
    "Powered by Brahmastra\n"
    "DIAGRAM:\n" + _MERMAID + "\n"
)


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    """Hermetic Settings with the image + diagram lanes ON, state under tmp_path."""
    base: dict[str, object] = {
        "IMAGE_ENABLED": True,
        "COUNCIL_IMAGE_ENABLED": True,
        "COUNCIL_DIAGRAM_ENABLED": True,
        "COUNCIL_IMAGE_EVERY_N": 1,
        "COUNCIL_IMAGE_DIR": str(tmp_path / "images"),
        "COUNCIL_IMAGE_STATE_PATH": str(tmp_path / ".council_image_state.json"),
        "IMAGE_MAX_PER_WEEK": 4,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


# --- 1. Compose parsing -----------------------------------------------------


def test_parse_extracts_mermaid_diagram_section() -> None:
    # Act: parse a composition that includes a DIAGRAM section.
    composed = _parse_composition(_RAW_WITH_DIAGRAM)
    # Assert: the mermaid source is captured verbatim, arrows intact.
    assert composed.diagram is not None
    assert composed.diagram.mermaid.startswith("flowchart TD")
    assert "Route to human" in composed.diagram.mermaid


def test_parse_drops_non_mermaid_diagram_body() -> None:
    # Arrange: a DIAGRAM section that is prose, not a mermaid diagram.
    raw = _RAW_WITH_DIAGRAM.replace(_MERMAID, "here is a picture of the flow")
    # Act
    composed = _parse_composition(raw)
    # Assert: not a real diagram → dropped, but the post survives.
    assert composed.diagram is None
    assert composed.post_text


def test_parse_diagram_strips_code_fence() -> None:
    # Arrange: the model wrapped the mermaid in a ```mermaid fence.
    fenced = ["```mermaid", *_MERMAID.splitlines(), "```"]
    # Act
    spec = _parse_diagram(fenced)
    # Assert: the fence is stripped; the source opens with the diagram keyword.
    assert spec is not None
    assert spec.mermaid.startswith("flowchart TD")
    assert "```" not in spec.mermaid


def test_parse_diagram_returns_none_for_empty_section() -> None:
    # Act / Assert: an empty section is no diagram, never an error.
    assert _parse_diagram([]) is None
    assert _parse_diagram(["   ", ""]) is None


# --- 2. Decision path -------------------------------------------------------


def test_decide_returns_diagram_when_present_and_enabled(tmp_path: Path) -> None:
    # Arrange: a diagram spec + the lane enabled, rotation set to skip everything.
    settings = _settings(tmp_path, COUNCIL_IMAGE_EVERY_N=999)
    spec = DiagramSpec(mermaid=_MERMAID)
    # Act
    choice = decide_council_image(
        "A technical post about grounding and abstaining.",
        diagram=spec,
        settings=settings,
    )
    # Assert: a diagram is CONTENT — it bypasses the every-N rotation entirely.
    assert choice.image_type == IMAGE_TYPE_DIAGRAM
    assert choice.diagram is spec


def test_decide_falls_back_when_diagram_lane_disabled(tmp_path: Path) -> None:
    # Arrange: a diagram spec but the diagram lane is OFF (rotation every post).
    settings = _settings(tmp_path, COUNCIL_DIAGRAM_ENABLED=False)
    spec = DiagramSpec(mermaid=_MERMAID)
    # Act
    choice = decide_council_image(
        "A reflective post with no crisp one-liner to pull as a quote.",
        diagram=spec,
        settings=settings,
    )
    # Assert: diagram ignored → the normal concept-illustration path is taken.
    assert choice.image_type == IMAGE_TYPE_CONCEPT


# --- 3. Renderer (mocked subprocess) ---------------------------------------


def _fake_mmdc(png: bytes):
    """Return a subprocess.run stand-in that writes ``png`` to the -o path."""

    def _run(cmd, **kwargs):  # noqa: ANN001 - matches subprocess.run signature loosely
        out_path = Path(cmd[cmd.index("-o") + 1])
        out_path.write_bytes(png)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    return _run


def test_render_mermaid_returns_png_bytes_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: mmdc resolves on PATH and writes a PNG to the output path.
    monkeypatch.setattr(diagram_renderer.shutil, "which", lambda _cmd: "mmdc")
    monkeypatch.setattr(diagram_renderer.subprocess, "run", _fake_mmdc(b"PNGDATA"))
    settings = _settings(tmp_path)
    # Act
    data = render_mermaid(_MERMAID, settings)
    # Assert
    assert data == b"PNGDATA"


def test_render_mermaid_raises_when_cli_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: the mermaid CLI is not on PATH.
    monkeypatch.setattr(diagram_renderer.shutil, "which", lambda _cmd: None)
    # Act / Assert
    with pytest.raises(DiagramRenderError):
        render_mermaid(_MERMAID, _settings(tmp_path))


def test_render_mermaid_raises_on_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: mmdc runs but exits non-zero (bad diagram syntax).
    monkeypatch.setattr(diagram_renderer.shutil, "which", lambda _cmd: "mmdc")

    def _fail(cmd, **kwargs):  # noqa: ANN001
        return types.SimpleNamespace(returncode=1, stdout="", stderr="Parse error")

    monkeypatch.setattr(diagram_renderer.subprocess, "run", _fail)
    # Act / Assert
    with pytest.raises(DiagramRenderError):
        render_mermaid(_MERMAID, _settings(tmp_path))


def test_render_mermaid_raises_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: mmdc hangs and hits the timeout.
    monkeypatch.setattr(diagram_renderer.shutil, "which", lambda _cmd: "mmdc")

    def _timeout(cmd, **kwargs):  # noqa: ANN001
        raise subprocess.TimeoutExpired(cmd, 60)

    monkeypatch.setattr(diagram_renderer.subprocess, "run", _timeout)
    # Act / Assert
    with pytest.raises(DiagramRenderError):
        render_mermaid(_MERMAID, _settings(tmp_path))


def test_render_mermaid_raises_on_empty_source(tmp_path: Path) -> None:
    # Act / Assert: an empty diagram is never rendered.
    with pytest.raises(DiagramRenderError):
        render_mermaid("   ", _settings(tmp_path))


def test_render_mermaid_wraps_io_error_as_diagram_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: the temp volume is full / read-only — TemporaryDirectory raises
    # OSError. The contract is that this degrades (DiagramRenderError), never a
    # bare OSError that would unwind through run_council and abort the post.
    monkeypatch.setattr(diagram_renderer.shutil, "which", lambda _cmd: "mmdc")

    class _BoomTempDir:
        def __init__(self, *args: object, **kwargs: object) -> None: ...
        def __enter__(self) -> str:
            raise OSError("no space left on device")

        def __exit__(self, *args: object) -> bool:
            return False

    monkeypatch.setattr(diagram_renderer.tempfile, "TemporaryDirectory", _BoomTempDir)
    # Act / Assert
    with pytest.raises(DiagramRenderError):
        render_mermaid(_MERMAID, _settings(tmp_path))


def test_parse_diagram_rejects_prefix_lookalike() -> None:
    # 'graphql' must NOT masquerade as the 'graph' diagram type.
    assert _parse_diagram(["graphql is a query language, not a diagram"]) is None


# --- 4. attach: end-to-end wiring + degrade ---------------------------------


def test_attach_writes_diagram_png_and_stamps_draft(tmp_path: Path) -> None:
    # Arrange: a draft carrying a diagram spec + an injected renderer (no mmdc).
    draft = {
        "id": "abc123",
        "post_text": "A technical post about grounding, abstaining, and audit.",
        "diagram": DiagramSpec(mermaid=_MERMAID),
    }
    settings = _settings(tmp_path)
    # Act
    choice = attach_council_image(
        draft, settings=settings, render_diagram=lambda _m, _s=None: b"PNGBYTES"
    )
    # Assert: the diagram image is chosen, written, and provenance recorded.
    assert choice.image_type == IMAGE_TYPE_DIAGRAM
    assert draft["image_type"] == IMAGE_TYPE_DIAGRAM
    assert draft["image_source"] == "mermaid"
    assert draft["image_prompt"] == _MERMAID
    assert Path(draft["image_path"]).read_bytes() == b"PNGBYTES"


def test_attach_degrades_to_text_only_on_render_failure(tmp_path: Path) -> None:
    # Arrange: the renderer fails (mmdc blew up) — the post must still ship.
    draft = {
        "id": "def456",
        "post_text": "A technical post that would have had a diagram.",
        "diagram": DiagramSpec(mermaid=_MERMAID),
    }

    def _boom(_mermaid, _settings=None):
        raise DiagramRenderError("mermaid exploded")

    settings = _settings(tmp_path)
    # Act
    choice = attach_council_image(draft, settings=settings, render_diagram=_boom)
    # Assert: degrade to text-only, never raise, never block the post.
    assert choice.image_type == IMAGE_TYPE_NONE
    assert draft["image_type"] == IMAGE_TYPE_NONE
    assert draft["image_path"] is None

"""Unit tests for the decoupled post -> diagram step (``vision.council.diagram``).

WHY these tests: the inline DIAGRAM: contract proved unreliable, so a dedicated
step reads the FINISHED post and asks a voice for one in-sync mermaid diagram.
These tests MOCK the voice transport - NO real model runs. We assert the
contract:

  1. a valid mermaid reply -> a DiagramSpec;
  2. an explicit 'NONE' reply (post has no diagrammable structure) -> None;
  3. non-mermaid junk -> None;
  4. a reply that leaks an AI/model name -> None (de-naming, fail-soft);
  5. a fenced ```mermaid reply is unwrapped;
  6. a voice that raises -> None (never blocks the post).

Every test is AAA with a single behavioural focus.
"""

from __future__ import annotations

from vision.council.compose import DiagramSpec
from vision.council.diagram import DiagramWriter, _parse_diagram_reply


_MERMAID = "flowchart TD\n  A[Query] --> B{Grounded?}\n  B -->|no| C[Route to human]"


class _FakeVoices:
    """A Voices stand-in that returns a canned reply (or raises) for ask()."""

    def __init__(self, reply: str | Exception) -> None:
        self._reply = reply
        self.calls = 0

    def ask(self, voice: str, prompt: str) -> str:
        self.calls += 1
        if isinstance(self._reply, Exception):
            raise self._reply
        return self._reply


def _writer(reply: str | Exception) -> DiagramWriter:
    return DiagramWriter(voices=_FakeVoices(reply))  # type: ignore[arg-type]


# --- _parse_diagram_reply (pure) -------------------------------------------


def test_parse_reply_accepts_valid_mermaid() -> None:
    spec = _parse_diagram_reply(_MERMAID)
    assert spec is not None
    assert spec.mermaid.startswith("flowchart TD")


def test_parse_reply_returns_none_for_none_sentinel() -> None:
    assert _parse_diagram_reply("NONE") is None
    assert _parse_diagram_reply("None.") is None


def test_parse_reply_returns_none_for_non_mermaid() -> None:
    assert _parse_diagram_reply("Here is a description of the flow, no diagram.") is None


def test_parse_reply_unwraps_code_fence() -> None:
    fenced = "```mermaid\n" + _MERMAID + "\n```"
    spec = _parse_diagram_reply(fenced)
    assert spec is not None and spec.mermaid.startswith("flowchart TD")


def test_parse_reply_drops_diagram_that_leaks_model_name() -> None:
    leaking = "flowchart TD\n  A[Ask Claude] --> B[Answer]"
    assert _parse_diagram_reply(leaking) is None


# --- DiagramWriter.diagram_for ---------------------------------------------


def test_diagram_for_returns_spec_on_valid_reply() -> None:
    spec = _writer(_MERMAID).diagram_for("A technical post about routing and abstaining.")
    assert isinstance(spec, DiagramSpec)


def test_diagram_for_returns_none_on_none_reply() -> None:
    assert _writer("NONE").diagram_for("A reflective post with no structure.") is None


def test_diagram_for_returns_none_on_empty_post() -> None:
    w = _writer(_MERMAID)
    assert w.diagram_for("   ") is None
    # No voice call is wasted on an empty post.
    assert w._voices.calls == 0  # type: ignore[attr-defined]


def test_diagram_for_swallows_voice_error() -> None:
    # Arrange: the transport blows up — a diagram is best-effort, never a blocker.
    spec = _writer(RuntimeError("cli died")).diagram_for("A technical post.")
    # Assert
    assert spec is None

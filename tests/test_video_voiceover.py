"""Unit tests for the TTS voice-over stage (BRD §18/§22 — tests are part of done).

WHY these tests: the voice-over stage makes two load-bearing promises the render
lane relies on —

  1. A multi-scene script yields a written mp3, a positive total duration, and one
     increasing ``scene_end_times`` per spoken scene (so captions/ducking can key
     off real audio boundaries).
  2. ANY edge-tts failure degrades to the typed ``VoiceoverError`` (never crashes
     the run), so the caller can fall back to a silent/text-only reel.

Every test is AAA (Arrange -> Act -> Assert), one behaviour each. The only
external collaborator, ``edge_tts.Communicate``, is MOCKED, so NO network call
ever happens and the suite is fully hermetic.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from vision.video.schema import ReelScript, Scene
from vision.video.voiceover import (
    VoiceoverError,
    VoiceoverResult,
    synthesize_voiceover,
)

# One second in edge-tts' 100-ns WordBoundary ticks. Named so fixtures read as
# real durations rather than opaque large integers.
_TICKS_PER_SECOND = 10_000_000


def _two_scene_script() -> ReelScript:
    """A minimal two-scene storyboard whose scenes both carry narration."""
    return ReelScript(
        title="Insight Reel",
        scenes=[
            Scene(image_prompt="a calm horizon", narration="First beat speaks."),
            Scene(image_prompt="a bright dawn", narration="Second beat speaks."),
        ],
        voice="en-US-AndrewNeural",
    )


class _FakeCommunicate:
    """Stand-in for ``edge_tts.Communicate`` that streams canned audio + timing.

    WHY a class (not a bare async gen): the real API is instantiated as
    ``Communicate(text, voice)`` then iterated via ``.stream()``. Mirroring that
    shape lets the test patch the class wholesale with no network involved. Each
    instance reports a fixed 1-second WordBoundary so per-scene durations are
    deterministic.
    """

    def __init__(self, text: str, voice: str) -> None:
        self._text = text
        self._voice = voice

    async def stream(self) -> AsyncIterator[dict[str, object]]:
        """Yield one audio chunk then one 1-second WordBoundary, like the real API."""
        yield {"type": "audio", "data": b"\xff\xf3mp3-frame-bytes"}
        yield {"type": "WordBoundary", "offset": 0, "duration": _TICKS_PER_SECOND}


class _FailingCommunicate:
    """Stand-in whose stream raises, simulating an edge-tts/network failure."""

    def __init__(self, text: str, voice: str) -> None:
        self._text = text
        self._voice = voice

    async def stream(self) -> AsyncIterator[dict[str, object]]:
        """Raise mid-stream to model a live edge-tts/network fault."""
        raise ConnectionError("simulated edge-tts endpoint failure")
        yield  # pragma: no cover — unreachable, keeps this an async generator


def test_synthesize_writes_audio_with_increasing_scene_end_times(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Arrange: patch the edge-tts client so no network call happens.
    monkeypatch.setattr(
        "vision.video.voiceover.edge_tts.Communicate", _FakeCommunicate
    )
    script = _two_scene_script()

    # Act
    result = synthesize_voiceover(script, out_dir=tmp_path)

    # Assert
    assert isinstance(result, VoiceoverResult)
    assert Path(result.audio_path).is_file()
    assert Path(result.audio_path).stat().st_size > 0
    assert result.duration_seconds > 0
    assert len(result.scene_end_times) == 2
    assert result.scene_end_times[0] < result.scene_end_times[1]


def test_synthesize_raises_voiceover_error_on_edge_tts_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Arrange: patch the client to fail mid-stream.
    monkeypatch.setattr(
        "vision.video.voiceover.edge_tts.Communicate", _FailingCommunicate
    )
    script = _two_scene_script()

    # Act / Assert
    with pytest.raises(VoiceoverError):
        synthesize_voiceover(script, out_dir=tmp_path)

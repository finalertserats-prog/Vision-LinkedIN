"""Typed stage contracts for the video lane (single source of truth, §22.5).

Every stage takes/returns one of these Pydantic models so boundaries are strict
and independently testable. NARRATION (spoken, may be TTS) is kept separate from
ON-SCREEN TEXT (deterministic captions/labels) — the precision guardrail (§23.3):
generative models never render a digit or a word into the frame.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Scene(BaseModel):
    """One reel beat: a text-free anime still + spoken line + on-screen caption."""

    image_prompt: str = Field(..., description="Text-free anime scene for agy (no words).")
    narration: str = Field(..., description="The spoken line for this scene (TTS).")
    on_screen_text: str = Field("", description="Deterministic caption burned into the frame.")
    duration_seconds: float = Field(4.0, ge=1.0, le=15.0)


class ReelScript(BaseModel):
    """The storyboard for one reel — scenes in order + audio-brand choices."""

    title: str
    scenes: list[Scene] = Field(..., min_length=1, max_length=8)
    voice: str = Field("en-US-AndrewNeural", description="edge-tts ShortName.")
    music_mood: str | None = Field(None, description="Mood tag for the licensed bed, or None.")

    @property
    def total_seconds(self) -> float:
        return sum(s.duration_seconds for s in self.scenes)

    @property
    def narration_text(self) -> str:
        """The full spoken script (for a single VO pass), scenes joined."""
        return " ".join(s.narration.strip() for s in self.scenes if s.narration.strip())


class VoiceoverResult(BaseModel):
    """TTS output: the audio file + per-scene timing for caption/ducking sync."""

    audio_path: str
    duration_seconds: float
    scene_end_times: list[float] = Field(default_factory=list)


class VideoAsset(BaseModel):
    """The rendered, web-safe MP4 (H.264/AAC, 1080x1920, +faststart)."""

    mp4_path: str
    width: int = 1080
    height: int = 1920
    duration_seconds: float
    size_bytes: int


class UploadResult(BaseModel):
    """The LinkedIn /rest/videos outcome — the video URN once AVAILABLE."""

    video_urn: str
    status: str  # 'AVAILABLE' | 'PROCESSING' | 'PROCESSING_FAILED'

"""Text-to-speech voice-over stage over the FREE edge-tts path (BRD §22 / video lane).

WHY this module exists: the reel needs a spoken narration track plus per-scene
timing so captions and music-ducking line up with the audio. The CONFIRMED-FREE,
no-API-key path is Microsoft's ``edge-tts`` (a public Edge read-aloud endpoint),
so the whole lane costs nothing and needs no secret (§22 — CLI/free-only).

WHY per-scene synthesis (not one whole-script pass): ``VoiceoverResult`` must
carry ``scene_end_times`` so the render stage can cut/caption on real audio
boundaries. Synthesising each scene separately gives an exact, measured duration
per beat; we then concatenate the mp3 chunks into one track. Durations are read
from edge-tts' own ``WordBoundary`` events (offset + duration, in 100-ns units)
rather than by decoding the mp3 — that keeps this stage free of heavy audio deps.

DEGRADE CONTRACT: any edge-tts/network failure raises ``VoiceoverError`` so the
caller can drop to a silent/text-only reel instead of crashing the run. Tokens
and secrets are never logged (there are none on this path, and we keep it that way).
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import tempfile
from pathlib import Path

import edge_tts
import imageio_ffmpeg

from vision.video.schema import ReelScript, Scene, VoiceoverResult

logger = logging.getLogger(__name__)

# edge-tts reports WordBoundary offsets/durations in 100-nanosecond ticks; divide
# by 1e7 to get seconds. Named so the conversion never reads as a magic number.
_TICKS_PER_SECOND = 1e7

# ffmpeg prints "Duration: HH:MM:SS.ss" on stderr. WHY we need this: edge-tts does
# NOT emit WordBoundary events for every voice (confirmed empty on a real run), so
# the tick-based measure silently returned 0. We fall back to probing the actual
# mp3 duration with the ffmpeg binary we already bundle (imageio-ffmpeg).
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


class VoiceoverError(Exception):
    """Raised when TTS synthesis fails for any reason (network, endpoint, no audio).

    WHY a dedicated type: the voice-over is a *degrade-gracefully* stage — a failed
    synthesis must not crash the pipeline. The caller catches this specifically to
    fall back to a silent/text-only reel, a different recovery than a hard content
    failure. Never carries a secret (this path has none) and never a stack trace.
    """


def synthesize_voiceover(
    script: ReelScript,
    *,
    settings: object | None = None,
    out_dir: str | Path | None = None,
) -> VoiceoverResult:
    """Synthesise the reel's narration and return the audio path + per-scene timing.

    Each scene's ``narration`` is synthesised to its own mp3 (to measure a real
    duration per beat), then all chunks are concatenated into one mp3 written to
    ``out_dir``. ``scene_end_times`` is the cumulative end time (seconds) after
    each scene, so downstream caption/ducking sync can key off audio boundaries.

    Args:
        script: The storyboard. ``script.voice`` selects the edge-tts ShortName.
        settings: Accepted for interface symmetry with the other video stages but
            currently unused — the free edge-tts path needs no config/secret. Kept
            so callers need not special-case this stage's signature.
        out_dir: Directory for the final mp3. Defaults to a fresh temp dir so the
            stage is usable without the caller pre-creating storage.

    Returns:
        A ``VoiceoverResult`` with the concatenated ``audio_path``, total
        ``duration_seconds``, and increasing ``scene_end_times`` (one per scene
        that produced audio).

    Raises:
        VoiceoverError: On ANY synthesis failure. The caller MUST catch this and
            degrade to a silent/text-only reel — TTS never blocks publishing.
    """
    # ``settings`` is intentionally ignored: edge-tts is keyless and needs no
    # config. Referencing it documents the deliberate no-op for readers.
    _ = settings

    target_dir = _resolve_out_dir(out_dir)

    audio_chunks: list[bytes] = []
    scene_end_times: list[float] = []
    cumulative_seconds = 0.0

    for index, scene in enumerate(script.scenes):
        audio, seconds = _synthesize_scene(scene, script.voice, index)
        if not audio:
            # A scene with no narration yields no audio and no timing boundary;
            # skip it so scene_end_times only marks beats that actually speak.
            continue
        audio_chunks.append(audio)
        cumulative_seconds += seconds
        scene_end_times.append(cumulative_seconds)

    if not audio_chunks:
        # No scene produced audio → there is nothing to voice. Fail-closed so the
        # caller degrades rather than returning a zero-byte "success".
        raise VoiceoverError("no narration produced any audio to synthesise")

    audio_path = _write_concatenated_mp3(audio_chunks, target_dir)

    return VoiceoverResult(
        audio_path=str(audio_path),
        duration_seconds=cumulative_seconds,
        scene_end_times=scene_end_times,
    )


def _resolve_out_dir(out_dir: str | Path | None) -> Path:
    """Return an existing output directory, defaulting to a fresh temp dir.

    WHY default to a temp dir: this stage must be runnable standalone (tests,
    ad-hoc synthesis) without the caller provisioning storage first. When a dir
    IS given we create it if missing so the caller need not pre-make it.
    """
    if out_dir is None:
        return Path(tempfile.mkdtemp(prefix="vision_voiceover_"))
    resolved = Path(out_dir)
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _synthesize_scene(
    scene: Scene, voice: str, index: int
) -> tuple[bytes, float]:
    """Synthesise one scene's narration, returning (mp3 bytes, duration seconds).

    Streams edge-tts once: audio chunks are accumulated into the mp3 bytes and
    WordBoundary events are tracked so the duration is the last spoken word's end
    (offset + duration) — an exact, decode-free measure of how long this beat runs.
    An empty/whitespace narration is a deliberate no-op returning ``(b"", 0.0)``.
    Any edge-tts/network error becomes ``VoiceoverError`` for graceful degradation.
    """
    text = scene.narration.strip()
    if not text:
        return b"", 0.0

    try:
        audio, seconds = asyncio.run(_stream_scene(text, voice))
    except VoiceoverError:
        # Already the typed contract error — surface as-is (don't double-wrap).
        raise
    except Exception as exc:  # noqa: BLE001 — funnel ALL edge-tts/async faults
        # WHY catch broadly here: edge-tts raises a wide, unstable set of network
        # and protocol errors we cannot enumerate; the stage's job is to turn ANY
        # of them into the single degrade signal. The message names the scene, not
        # any secret (this path has none).
        raise VoiceoverError(
            f"edge-tts synthesis failed for scene {index}: {exc}"
        ) from exc

    # WordBoundary can be absent for a voice (real-run finding); when the tick
    # measure is unusable, probe the actual audio so scene timing is never zero.
    if seconds <= 0.0:
        seconds = _probe_duration_seconds(audio)
    return audio, seconds


def _probe_duration_seconds(mp3_bytes: bytes) -> float:
    """Measure an mp3's real duration (seconds) via the bundled ffmpeg, or 0.0.

    The reliable fallback when edge-tts emits no WordBoundary events. Writes the
    bytes to a temp file and parses ``Duration:`` from ``ffmpeg -i`` stderr (the
    imageio-ffmpeg binary ships no ffprobe). Best-effort: any failure returns 0.0
    so timing degrades rather than crashing the reel.
    """
    if not mp3_bytes:
        return 0.0
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp.write(mp3_bytes)
            tmp_path = tmp.name
        proc = subprocess.run(  # noqa: S603 — args are the bundled binary + our temp path
            [imageio_ffmpeg.get_ffmpeg_exe(), "-i", tmp_path],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        match = _DURATION_RE.search(proc.stderr)
        if not match:
            return 0.0
        hours, minutes, seconds = match.groups()
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        logger.warning("mp3 duration probe failed (%s); scene timing may be off.", type(exc).__name__)
        return 0.0
    finally:
        if tmp_path is not None:
            try:
                Path(tmp_path).unlink()
            except OSError:
                pass


async def _stream_scene(text: str, voice: str) -> tuple[bytes, float]:
    """Stream one edge-tts synthesis, returning accumulated mp3 bytes + duration.

    WHY ``stream()`` over ``save()``: streaming yields both ``audio`` chunks (the
    mp3 bytes) and ``WordBoundary`` events in one pass, letting us build the file
    AND measure its spoken length without re-reading/decoding the mp3.
    """
    communicate = edge_tts.Communicate(text, voice)

    audio_buffer = bytearray()
    last_end_ticks = 0

    async for chunk in communicate.stream():
        chunk_type = chunk.get("type")
        if chunk_type == "audio":
            audio_buffer.extend(chunk["data"])
        elif chunk_type == "WordBoundary":
            # The spoken length is the final word's end = offset + its duration.
            last_end_ticks = chunk["offset"] + chunk["duration"]

    if not audio_buffer:
        # A stream that yielded no audio bytes is a failed synthesis, not silence.
        raise VoiceoverError("edge-tts stream returned no audio data")

    duration_seconds = last_end_ticks / _TICKS_PER_SECOND
    return bytes(audio_buffer), duration_seconds


def _write_concatenated_mp3(audio_chunks: list[bytes], target_dir: Path) -> Path:
    """Concatenate per-scene mp3 chunks into one file and return its path.

    WHY raw byte concatenation is safe here: every chunk is an independent mp3
    frame stream from the same edge-tts voice/codec, so appending them yields a
    single playable mp3 without re-encoding — keeping this stage dependency-light.
    """
    audio_path = target_dir / "voiceover.mp3"
    try:
        audio_path.write_bytes(b"".join(audio_chunks))
    except OSError as exc:
        # A write failure is unrecoverable for this stage → degrade signal.
        raise VoiceoverError(
            f"failed to write voice-over mp3 to {audio_path}: {exc}"
        ) from exc
    return audio_path

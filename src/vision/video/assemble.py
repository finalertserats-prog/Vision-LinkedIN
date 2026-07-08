"""ffmpeg ASSEMBLY stage — anime stills → one web-safe Insight Reel (Phase 5).

WHY this module exists: the reel's moving picture is *composited*, not generated.
agy hands us text-free anime STILLS; the spoken NARRATION is a separate VO track;
and the ON-SCREEN TEXT is a deterministic caption we burn in here — the precision
guardrail (§23.3): a diffusion model never renders a word or a digit into a frame,
so every caption is drawn crisply by ffmpeg's ``drawtext`` from the exact string.

The stage builds ONE ffmpeg invocation (per the D10 "no browser, headless" rule)
that, for each still: applies a subtle Ken Burns ``zoompan`` (slow zoom) scaled and
cropped to FILL the 1080x1920 portrait canvas, holds it for its ``duration_seconds``
at the configured fps, and burns the caption near the lower third. The clips are
concatenated in order, the VO is laid as the primary audio track (music, if given,
is ducked UNDER it), and the result is exported H.264/AAC, yuv420p, ``+faststart``
so the moov atom is at the front for progressive playback.

The ffmpeg binary comes from ``imageio-ffmpeg`` (NO API key, NO system install) —
never assumed on PATH. Any non-zero ffmpeg exit raises ``AssemblyError`` with a
bounded stderr tail (no secrets), so a broken render fails loudly, never silently.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import imageio_ffmpeg
from matplotlib import font_manager

from vision.config import Settings, get_settings
from vision.video.schema import VideoAsset

logger = logging.getLogger(__name__)

# --- Encode contract (the web-safe MP4 LinkedIn accepts) --------------------
# WHY named constants: these are the delivery contract the caller/tests assert
# against; magic strings scattered through the command would be unreadable (§22).
_PIX_FMT = "yuv420p"  # widest-compatible chroma; some players reject others
_VIDEO_CODEC = "libx264"
_AUDIO_CODEC = "aac"
_X264_PRESET = "medium"
_X264_CRF = "20"  # visually-lossless-ish for stills; small files
_AUDIO_BITRATE = "192k"
# The music bed sits ~18 dB UNDER the voiceover so narration always dominates.
_MUSIC_DUCK_DB = -18.0
# How much of a captured ffmpeg stderr we surface on failure — enough to debug,
# bounded so a runaway log (or an echoed path) can never flood the exception.
_STDERR_TAIL_CHARS = 2000

# Ken Burns: total zoom travelled across a scene (1.0 → this). Deliberately tiny
# so the motion reads as a slow drift, not a lurch (owner's "subtle" brief).
_KENBURNS_END_ZOOM = 1.08
# We render zoompan on an OVER-SCALED frame (canvas * this) so the slow zoom stays
# crisp — zoompan upscaling a 1x frame softens edges.
_KENBURNS_SUPERSAMPLE = 2

# --- Caption (drawtext) geometry -------------------------------------------
# WHY lower-third: the caption must not cover the anime subject (usually centred)
# and must clear the platform's bottom UI chrome. These are fractions of height.
_CAPTION_FONT_FRACTION = 0.045  # font size as a fraction of canvas height
_CAPTION_Y_FRACTION = 0.78  # caption baseline as a fraction of canvas height
_CAPTION_BOX_ALPHA = 0.55  # semi-opaque backing box for legibility over any still
_CAPTION_BORDER_W = 2  # black outline so text stays crisp on light frames
# A gentle open-from-black / close-to-black on the whole reel for a finished feel.
# A clean, low-risk polish that reads far more "produced" than a hard cut in/out.
_VIDEO_FADE_SECONDS = 0.5


class AssemblyError(Exception):
    """Raised when the ffmpeg assembly fails (non-zero exit).

    Carries a bounded, secret-free tail of ffmpeg's stderr so a failure is
    debuggable without dumping the entire (potentially path-leaking) log.
    """


@dataclass(frozen=True)
class SceneClip:
    """One assembled reel beat: a still image + its burnt-in caption + hold time.

    This is the ASSEMBLY-stage view of a ``schema.Scene`` — the upstream stage
    has already resolved the ``image_prompt`` to a rendered ``image_path``. Kept
    as a small frozen record so the input to :func:`assemble_reel` is explicit and
    immutable (§ immutability), rather than a bare positional tuple at the call site.
    """

    image_path: Path
    on_screen_text: str
    duration_seconds: float


# The public input shape: each scene is either a ready ``SceneClip`` or the plain
# ``(image_path, on_screen_text, duration_seconds)`` tuple it is built from. The
# tuple form keeps callers that don't want to import the dataclass ergonomic.
SceneInput = SceneClip | tuple[str | os.PathLike[str], str, float]


def _resolve_font_path() -> str:
    """Return an absolute path to a bundled DejaVu Sans .ttf for ``drawtext``.

    Uses matplotlib's ``font_manager`` (matplotlib is a hard dependency, so the
    font is guaranteed present) — identical text rendering across machines, no
    reliance on a system font that may be missing in a headless cron box (D10).
    """
    return font_manager.findfont(font_manager.FontProperties(family="DejaVu Sans"))


def _escape_drawtext_path(path: str) -> str:
    r"""Escape a filesystem path for use in an ffmpeg ``drawtext=fontfile=`` value.

    ffmpeg's filtergraph parser treats ``\``, ``:`` and ``'`` specially, and a
    Windows path (``C:\...\DejaVuSans.ttf``) contains both a drive colon and
    backslashes — unescaped they would break the graph. WHY a dedicated helper:
    this escaping is subtle and easy to get wrong, so it lives in one tested place.
    """
    # Backslash first (so we don't double-escape our own inserted escapes), then
    # the drive colon, then any single quote.
    return path.replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def _escape_drawtext_text(text: str) -> str:
    r"""Escape arbitrary caption ``text`` for an ffmpeg ``drawtext=text='...'``.

    Neutralises the metacharacters ffmpeg expands inside a quoted drawtext value
    (``\``, ``'``, ``:``, ``%``) and flattens newlines to spaces, so a caption is
    rendered *literally* — the deterministic-text guarantee (§23.3). Without this,
    a caption containing ``%`` (a strftime/expansion token) or a colon would render
    garbled or crash the graph.
    """
    flattened = " ".join(text.split())  # collapse newlines/runs of whitespace
    return (
        flattened.replace("\\", r"\\")
        .replace("'", r"\'")
        .replace(":", r"\:")
        .replace("%", r"\%")
    )


def _normalise_scene(scene: SceneInput) -> SceneClip:
    """Coerce a scene tuple into a validated :class:`SceneClip`.

    Accepts either the frozen dataclass or the documented
    ``(image_path, on_screen_text, duration_seconds)`` tuple. Validates the image
    exists and the duration is positive HERE (the single boundary), so ffmpeg is
    never handed a missing file or a zero-frame clip — both of which fail obscurely
    deep inside the filtergraph rather than with a clear message.
    """
    if isinstance(scene, SceneClip):
        clip = scene
    else:
        image_path, on_screen_text, duration_seconds = scene
        clip = SceneClip(Path(image_path), on_screen_text, float(duration_seconds))

    if clip.duration_seconds <= 0:
        raise AssemblyError(
            f"scene for {clip.image_path!s} has non-positive duration "
            f"{clip.duration_seconds}; every clip must hold for >0s"
        )
    if not Path(clip.image_path).is_file():
        raise AssemblyError(f"scene still not found: {clip.image_path!s}")
    return clip


def _scene_filter(
    index: int,
    clip: SceneClip,
    *,
    width: int,
    height: int,
    fps: int,
    font_path: str,
) -> str:
    """Build the per-scene filtergraph: Ken Burns cover-fill + burnt-in caption.

    Pipeline for input stream ``[index]`` (a looped still):
      1. ``scale``+``crop`` to COVER an over-sampled canvas (so the zoom stays
         crisp), forcing the portrait aspect regardless of the still's shape.
      2. ``zoompan`` — a slow linear zoom to ``_KENBURNS_END_ZOOM`` over exactly
         ``duration * fps`` frames, output at the final ``width x height``.
      3. ``setsar=1`` + ``fps`` — square pixels and an exact frame cadence so the
         later ``concat`` sees uniform streams.
      4. ``drawtext`` — the deterministic caption over a semi-opaque box in the
         lower third (skipped when the scene has no on-screen text).

    Returns the graph for this scene labelled ``[v{index}]`` for the concat stage.
    """
    frames = max(1, round(clip.duration_seconds * fps))
    big_w, big_h = width * _KENBURNS_SUPERSAMPLE, height * _KENBURNS_SUPERSAMPLE

    # Cover-fill: scale up preserving aspect (force_original_aspect_ratio=increase)
    # then centre-crop to the exact over-sampled box — no letterboxing, ever.
    cover = (
        f"scale={big_w}:{big_h}:force_original_aspect_ratio=increase,"
        f"crop={big_w}:{big_h}"
    )
    # Linear zoom from 1.0 to end-zoom across the clip; pan holds centre.
    zoom_expr = f"min(zoom+{(_KENBURNS_END_ZOOM - 1.0):.6f}/{frames},{_KENBURNS_END_ZOOM})"
    kenburns = (
        f"zoompan=z='{zoom_expr}':d={frames}"
        f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":s={width}x{height}:fps={fps}"
    )
    chain = f"[{index}:v]{cover},{kenburns},setsar=1,fps={fps}"

    caption = clip.on_screen_text.strip()
    if caption:
        font_size = max(1, round(height * _CAPTION_FONT_FRACTION))
        caption_y = round(height * _CAPTION_Y_FRACTION)
        drawtext = (
            f"drawtext=fontfile='{_escape_drawtext_path(font_path)}'"
            f":text='{_escape_drawtext_text(caption)}'"
            f":fontcolor=white:fontsize={font_size}"
            f":borderw={_CAPTION_BORDER_W}:bordercolor=black@1.0"
            f":box=1:boxcolor=black@{_CAPTION_BOX_ALPHA}:boxborderw=24"
            f":x=(w-text_w)/2:y={caption_y}"
        )
        chain = f"{chain},{drawtext}"

    return f"{chain}[v{index}]"


def _build_filter_complex(
    clips: list[SceneClip],
    *,
    width: int,
    height: int,
    fps: int,
    font_path: str,
    audio_input_indexes: tuple[int, int] | tuple[int],
) -> str:
    """Assemble the full ``-filter_complex`` graph: scenes → concat → audio mix.

    Video: each scene's chain (see :func:`_scene_filter`) feeds a single
    ``concat`` that joins them in order into ``[vout]``. Audio: if a music index is
    present, the VO and the ducked music are mixed with ``amix`` into ``[aout]``;
    otherwise the VO is passed through unchanged. Returning one string keeps the
    whole render a SINGLE ffmpeg invocation (per the headless, one-shot brief).
    """
    scene_graphs = [
        _scene_filter(i, clip, width=width, height=height, fps=fps, font_path=font_path)
        for i, clip in enumerate(clips)
    ]
    concat_inputs = "".join(f"[v{i}]" for i in range(len(clips)))
    concat = f"{concat_inputs}concat=n={len(clips)}:v=1:a=0[vcat]"
    # Gentle fade from black at the open and to black at the close of the whole reel
    # (clamped so a very short reel still fades). This runs on the concatenated
    # stream so the timing is the reel's real total duration.
    total = sum(c.duration_seconds for c in clips)
    fade = min(_VIDEO_FADE_SECONDS, total / 4)
    vfade = (
        f"[vcat]fade=t=in:st=0:d={fade:.3f},"
        f"fade=t=out:st={max(0.0, total - fade):.3f}:d={fade:.3f}[vout]"
    )

    if len(audio_input_indexes) == 2:
        vo_index, music_index = audio_input_indexes
        # Duck the music, then mix under the VO. ``amix`` normalises by input
        # count; ``normalize=0`` keeps the VO at full level so it stays dominant,
        # and ``dropout_transition=0`` avoids a level pump when the shorter track
        # (music trimmed to VO length) ends.
        audio = (
            f"[{music_index}:a]volume={_MUSIC_DUCK_DB}dB[music];"
            f"[{vo_index}:a][music]amix=inputs=2:duration=first"
            f":dropout_transition=0:normalize=0[aout]"
        )
    else:
        (vo_index,) = audio_input_indexes
        audio = f"[{vo_index}:a]anull[aout]"

    return ";".join([*scene_graphs, concat, vfade, audio])


def _build_command(
    ffmpeg_exe: str,
    clips: list[SceneClip],
    *,
    voiceover_audio_path: Path,
    music_path: Path | None,
    out_path: Path,
    width: int,
    height: int,
    fps: int,
    font_path: str,
) -> list[str]:
    """Build the full argv for the single ffmpeg render.

    Each still is a looped image input held for its ``duration_seconds`` (so the
    ``zoompan`` has enough source frames), followed by the VO (and optional music)
    audio inputs. The mapped ``[vout]``/``[aout]`` are encoded to the delivery
    contract with ``+faststart`` and ``-shortest`` so the file ends with the video.
    """
    command: list[str] = [ffmpeg_exe, "-hide_banner", "-y"]

    for clip in clips:
        # ``-loop 1 -t D`` makes a single still into a D-second stream; the fps is
        # applied in the filtergraph so the input framerate here is unimportant.
        command += ["-loop", "1", "-t", f"{clip.duration_seconds}", "-i", str(clip.image_path)]

    vo_index = len(clips)
    command += ["-i", str(voiceover_audio_path)]
    if music_path is not None:
        music_index = vo_index + 1
        command += ["-i", str(music_path)]
        audio_indexes: tuple[int, int] | tuple[int] = (vo_index, music_index)
    else:
        audio_indexes = (vo_index,)

    filter_complex = _build_filter_complex(
        clips,
        width=width,
        height=height,
        fps=fps,
        font_path=font_path,
        audio_input_indexes=audio_indexes,
    )

    command += [
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-map", "[aout]",
        "-c:v", _VIDEO_CODEC,
        "-preset", _X264_PRESET,
        "-crf", _X264_CRF,
        "-pix_fmt", _PIX_FMT,
        "-r", str(fps),
        "-c:a", _AUDIO_CODEC,
        "-b:a", _AUDIO_BITRATE,
        "-movflags", "+faststart",
        "-shortest",
        str(out_path),
    ]
    return command


def _run_ffmpeg(command: list[str]) -> None:
    """Run the ffmpeg command, raising :class:`AssemblyError` on non-zero exit.

    stderr is captured (never streamed to the console) so a failure surfaces a
    bounded, secret-free tail via the exception rather than flooding logs. WHY the
    tail is bounded: ffmpeg's stderr can be thousands of lines and may echo input
    paths — we keep only the last ``_STDERR_TAIL_CHARS`` for diagnosis.
    """
    logger.debug("ffmpeg assembly: %d args", len(command))
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        tail = (result.stderr or "")[-_STDERR_TAIL_CHARS:]
        raise AssemblyError(
            f"ffmpeg exited {result.returncode} during reel assembly. "
            f"stderr tail:\n{tail}"
        )


def _probe_duration(ffmpeg_exe: str, media_path: Path, fallback: float) -> float:
    """Return ``media_path``'s duration in seconds, probed via ffmpeg.

    ``imageio-ffmpeg`` ships ffmpeg but NOT ffprobe, so we probe by running ffmpeg
    with no output and scraping the ``Duration: HH:MM:SS.ff`` line from stderr. If
    the probe fails or the line is absent, we fall back to the summed scene
    durations — a real number is always returned, never a crash on a soft failure.
    """
    result = subprocess.run(
        [ffmpeg_exe, "-hide_banner", "-i", str(media_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    for line in (result.stderr or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("Duration:"):
            token = stripped.split("Duration:", 1)[1].split(",", 1)[0].strip()
            try:
                hours, minutes, seconds = token.split(":")
                return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
            except (ValueError, IndexError):
                break
    logger.warning("could not probe duration of %s; using summed scene time", media_path)
    return fallback


def _default_out_path(settings: Settings) -> Path:
    """Choose an output path under ``settings.video_work_dir`` (temp fallback).

    Prefers the configured work dir (config over code, §22.6). If that directory
    cannot be created (e.g. a read-only test box), falls back to a unique file in
    the system temp dir so the render always has a valid target.
    """
    work_dir = Path(settings.video_work_dir)
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        return work_dir / "insight_reel.mp4"
    except OSError:
        handle, temp_name = tempfile.mkstemp(prefix="insight_reel_", suffix=".mp4")
        os.close(handle)
        return Path(temp_name)


def assemble_reel(
    scenes: list[SceneInput],
    voiceover_audio_path: str | os.PathLike[str],
    *,
    settings: Settings | None = None,
    out_path: str | os.PathLike[str] | None = None,
    music_path: str | os.PathLike[str] | None = None,
) -> VideoAsset:
    """Assemble anime stills + VO (+ optional music) into one web-safe reel MP4.

    Builds a SINGLE ffmpeg invocation that Ken-Burns-pans each still to fill the
    configured portrait canvas, burns the deterministic caption into the lower
    third, concatenates the scenes in order, lays the VO as the primary audio
    (ducking any music UNDER it), and exports H.264/AAC, yuv420p, ``+faststart``.

    Args:
        scenes: Ordered reel beats. Each is EITHER a :class:`SceneClip` OR a
            ``(image_path, on_screen_text, duration_seconds)`` tuple — the still to
            show, the caption to burn in, and how long to hold it (seconds).
        voiceover_audio_path: The primary spoken-narration audio track.
        settings: Config source (canvas dims, fps, work dir); defaults to the
            process singleton.
        out_path: Where to write the MP4; defaults to ``settings.video_work_dir``
            (or a temp file if that dir is unwritable).
        music_path: Optional licensed music bed, mixed ~18 dB under the VO.

    Returns:
        A :class:`VideoAsset` with the real, probed ``duration_seconds`` and the
        on-disk ``size_bytes``.

    Raises:
        AssemblyError: If ``scenes`` is empty, a still is missing, a duration is
            non-positive, or ffmpeg exits non-zero (with a bounded stderr tail).
    """
    settings = settings or get_settings()

    if not scenes:
        raise AssemblyError("assemble_reel requires at least one scene")

    clips = [_normalise_scene(scene) for scene in scenes]

    voiceover = Path(voiceover_audio_path)
    if not voiceover.is_file():
        raise AssemblyError(f"voiceover audio not found: {voiceover!s}")

    music = Path(music_path) if music_path is not None else None
    if music is not None and not music.is_file():
        raise AssemblyError(f"music bed not found: {music!s}")

    target = Path(out_path) if out_path is not None else _default_out_path(settings)
    target.parent.mkdir(parents=True, exist_ok=True)

    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    font_path = _resolve_font_path()
    width, height, fps = settings.video_width, settings.video_height, settings.video_fps

    command = _build_command(
        ffmpeg_exe,
        clips,
        voiceover_audio_path=voiceover,
        music_path=music,
        out_path=target,
        width=width,
        height=height,
        fps=fps,
        font_path=font_path,
    )
    _run_ffmpeg(command)

    if not target.is_file() or target.stat().st_size == 0:
        raise AssemblyError(f"ffmpeg reported success but produced no output at {target!s}")

    summed = sum(clip.duration_seconds for clip in clips)
    duration = _probe_duration(ffmpeg_exe, target, fallback=summed)

    return VideoAsset(
        mp4_path=str(target),
        width=width,
        height=height,
        duration_seconds=duration,
        size_bytes=target.stat().st_size,
    )

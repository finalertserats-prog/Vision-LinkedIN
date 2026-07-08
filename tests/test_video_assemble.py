"""Tests for the ffmpeg ASSEMBLY stage (``vision.video.assemble``).

Two layers, per the house TDD style:
  * A REAL render integration test — ffmpeg is bundled (``imageio-ffmpeg``), so we
    synth tiny inputs and assert the actual MP4 is a valid 1080x1920 video stream.
  * Fast unit tests for the fail-loud contract (missing input → ``AssemblyError``)
    and the drawtext escaping helpers (the deterministic-text guardrail).

Durations are kept <=0.5s so the real render stays fast.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import imageio_ffmpeg
import pytest
from PIL import Image

from vision.config import get_settings
from vision.video.assemble import (
    AssemblyError,
    SceneClip,
    _escape_drawtext_path,
    _escape_drawtext_text,
    assemble_reel,
)


def _make_still(path: Path, color: tuple[int, int, int]) -> Path:
    """Write a tiny solid-colour PNG still (odd size, to exercise cover-crop)."""
    Image.new("RGB", (200, 356), color).save(path)
    return path


def _make_silence(path: Path, seconds: float) -> Path:
    """Synthesise a short silent WAV via the bundled ffmpeg (anullsrc)."""
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run(
        [ffmpeg_exe, "-hide_banner", "-y", "-f", "lavfi", "-i",
         "anullsrc=r=44100:cl=stereo", "-t", str(seconds), str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    )
    return path


def _probe_video_dimensions(path: Path) -> tuple[int, int]:
    """Scrape ``WxH`` for the video stream from bundled ffmpeg's stderr."""
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    result = subprocess.run(
        [ffmpeg_exe, "-hide_banner", "-i", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
    )
    for line in result.stderr.splitlines():
        if "Video:" in line:
            for token in line.replace(",", " ").split():
                if "x" in token and token.replace("x", "").isdigit():
                    width, height = token.split("x")
                    return int(width), int(height)
    raise AssertionError(f"no video stream dimensions found in:\n{result.stderr}")


@pytest.mark.integration
def test_assemble_reel_renders_valid_1080x1920_mp4(tmp_path: Path) -> None:
    # Arrange — two tiny stills, a captioned scene list, and a short VO.
    still_a = _make_still(tmp_path / "a.png", (200, 40, 40))
    still_b = _make_still(tmp_path / "b.png", (40, 80, 200))
    voiceover = _make_silence(tmp_path / "vo.wav", 0.6)
    scenes = [
        (str(still_a), "First Insight", 0.3),
        SceneClip(still_b, "Second Insight", 0.3),
    ]
    out_path = tmp_path / "reel.mp4"

    # Act
    asset = assemble_reel(scenes, str(voiceover), out_path=str(out_path))

    # Assert — a real, non-empty, correctly-sized H.264 portrait video exists.
    assert Path(asset.mp4_path).is_file()
    assert asset.size_bytes > 0
    assert _probe_video_dimensions(out_path) == (1080, 1920)
    assert asset.duration_seconds > 0


@pytest.mark.integration
def test_assemble_reel_mixes_music_under_voiceover(tmp_path: Path) -> None:
    # Arrange — a still, a VO, and a separate (silent) music bed to duck.
    still = _make_still(tmp_path / "s.png", (10, 120, 90))
    voiceover = _make_silence(tmp_path / "vo.wav", 0.5)
    music = _make_silence(tmp_path / "music.wav", 2.0)

    # Act
    asset = assemble_reel(
        [(str(still), "Ducked", 0.4)],
        str(voiceover),
        out_path=str(tmp_path / "reel.mp4"),
        music_path=str(music),
    )

    # Assert
    assert Path(asset.mp4_path).is_file()
    assert asset.size_bytes > 0


def test_assemble_reel_raises_on_missing_still(tmp_path: Path) -> None:
    # Arrange — a valid VO but a still path that does not exist.
    voiceover = _make_silence(tmp_path / "vo.wav", 0.3)
    scenes = [(str(tmp_path / "does_not_exist.png"), "caption", 0.3)]

    # Act / Assert
    with pytest.raises(AssemblyError, match="still not found"):
        assemble_reel(scenes, str(voiceover), out_path=str(tmp_path / "out.mp4"))


def test_assemble_reel_raises_on_ffmpeg_failure_bad_audio(tmp_path: Path) -> None:
    # Arrange — a real still but a bogus (non-media) audio file: ffmpeg exits non-zero.
    still = _make_still(tmp_path / "s.png", (0, 0, 0))
    bad_audio = tmp_path / "not_audio.wav"
    bad_audio.write_text("this is not audio")

    # Act / Assert — the ffmpeg non-zero exit surfaces as AssemblyError.
    with pytest.raises(AssemblyError, match="ffmpeg exited"):
        assemble_reel(
            [(str(still), "x", 0.3)], str(bad_audio), out_path=str(tmp_path / "out.mp4")
        )


def test_assemble_reel_raises_on_empty_scenes(tmp_path: Path) -> None:
    # Arrange
    voiceover = _make_silence(tmp_path / "vo.wav", 0.3)

    # Act / Assert
    with pytest.raises(AssemblyError, match="at least one scene"):
        assemble_reel([], str(voiceover))


def test_assemble_reel_raises_on_non_positive_duration(tmp_path: Path) -> None:
    # Arrange
    still = _make_still(tmp_path / "s.png", (5, 5, 5))
    voiceover = _make_silence(tmp_path / "vo.wav", 0.3)

    # Act / Assert
    with pytest.raises(AssemblyError, match="non-positive duration"):
        assemble_reel([(str(still), "x", 0.0)], str(voiceover))


def test_escape_drawtext_text_neutralises_metacharacters() -> None:
    # Arrange
    raw = "Revenue: 50% up\nnow"

    # Act
    escaped = _escape_drawtext_text(raw)

    # Assert — colon, percent escaped; newline collapsed to a space.
    assert r"\:" in escaped
    assert r"\%" in escaped
    assert "\n" not in escaped


def test_escape_drawtext_path_escapes_windows_drive_colon() -> None:
    # Arrange
    raw = r"C:\fonts\DejaVuSans.ttf"

    # Act
    escaped = _escape_drawtext_path(raw)

    # Assert — path separators normalised to forward slashes and the drive colon
    # escaped as ``\:`` (the only backslash left is the one escaping that colon).
    assert "/fonts/DejaVuSans.ttf" in escaped
    assert r"C\:/" in escaped


def test_get_settings_supplies_portrait_canvas_defaults() -> None:
    # A guard so the render contract (1080x1920 @ 30fps) can't silently drift.
    settings = get_settings()
    assert settings.video_width == 1080
    assert settings.video_height == 1920
    assert settings.video_fps == 30


@pytest.mark.integration
def test_later_scene_actually_appears_in_the_reel(tmp_path) -> None:
    # Regression (2026-07-08): a looped-still input made zoompan multiply frames so
    # the FIRST scene filled the whole reel and later scenes never played. Render a
    # 2-scene reel (solid RED then solid BLUE) and assert a late frame is BLUE.
    import subprocess

    import imageio_ffmpeg
    from PIL import Image

    from vision.video.assemble import assemble_reel

    ff = imageio_ffmpeg.get_ffmpeg_exe()
    red = tmp_path / "red.png"
    blue = tmp_path / "blue.png"
    Image.new("RGB", (300, 356), (220, 20, 20)).save(red)
    Image.new("RGB", (300, 356), (20, 20, 220)).save(blue)
    wav = tmp_path / "sil.wav"
    subprocess.run([ff, "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", "1.2", str(wav)],
                   capture_output=True, check=True)

    out = tmp_path / "reel.mp4"
    assemble_reel([(str(red), "", 0.6), (str(blue), "", 0.6)], str(wav),
                  out_path=str(out))

    frame = tmp_path / "late.png"
    subprocess.run([ff, "-y", "-ss", "0.9", "-i", str(out), "-frames:v", "1", str(frame)],
                   capture_output=True, check=True)
    with Image.open(frame) as im:
        r, g, b = im.convert("RGB").resize((1, 1)).getpixel((0, 0))
    assert b > r, f"late frame should be the BLUE (2nd) scene, got rgb=({r},{g},{b})"

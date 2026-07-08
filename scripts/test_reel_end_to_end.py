"""End-to-end video smoke test: real anime stills (agy) + real TTS (edge-tts) +
real ffmpeg assembly -> a watchable 1080x1920 MP4. No API key anywhere.
"""

from __future__ import annotations

from pathlib import Path

from vision.brahmastra.image_client import BrahmastraImageClient
from vision.config import get_settings
from vision.video.assemble import assemble_reel
from vision.video.schema import ReelScript, Scene
from vision.video.voiceover import synthesize_voiceover


def main() -> int:
    settings = get_settings()
    work = Path("prep/reel_test")
    work.mkdir(parents=True, exist_ok=True)

    # A short reel from one of our own "problems we overcame" stories.
    script = ReelScript(
        title="The bug that only broke in Gmail",
        scenes=[
            Scene(
                image_prompt="a developer frowning at a laptop that shows a broken image icon, moody dim room",
                narration="Our approval emails kept showing a broken image, but only in Gmail.",
                on_screen_text="ONLY IN GMAIL",
                duration_seconds=4.0,
            ),
            Scene(
                image_prompt="a detective with a magnifying glass leaning over glowing lines of code, investigative",
                narration="The image was fine everywhere else. Gmail silently strips inline data URIs.",
                on_screen_text="GMAIL STRIPS DATA URIS",
                duration_seconds=4.0,
            ),
            Scene(
                image_prompt="a calm lightbulb moment at a tidy desk at dawn, warm resolution",
                narration="The fix was a cid attachment. The lesson: when a bug lives in one place, that place is the diagnosis.",
                on_screen_text="THAT PLACE IS THE DIAGNOSIS",
                duration_seconds=5.0,
            ),
        ],
        voice="en-US-AndrewNeural",
    )

    print("1/3 synthesizing voice-over (edge-tts, no key)...")
    vo = synthesize_voiceover(script, settings=settings, out_dir=str(work))
    print(f"    voice: {vo.duration_seconds:.1f}s -> {vo.audio_path}")
    print(f"    scene end times: {[round(t, 1) for t in vo.scene_end_times]}")

    print("2/3 generating anime stills (agy)...")
    client = BrahmastraImageClient(settings, timeout=300.0)
    scenes_input: list[tuple[str, str, float]] = []
    prev = 0.0
    for i, scene in enumerate(script.scenes):
        img = client.illustrate(scene.image_prompt)
        p = work / f"scene_{i}.png"
        p.write_bytes(img)
        # Sync each still's on-screen time to when its narration ends (fallback to
        # the scripted duration if VO timing is short a scene).
        end = vo.scene_end_times[i] if i < len(vo.scene_end_times) else prev + scene.duration_seconds
        dur = max(1.5, end - prev)
        prev = end
        scenes_input.append((str(p), scene.on_screen_text, dur))
        print(f"    scene {i}: {len(img)} bytes, {dur:.1f}s on screen")

    print("3/3 assembling reel (ffmpeg)...")
    asset = assemble_reel(
        scenes_input, vo.audio_path, settings=settings, out_path=str(work / "reel.mp4")
    )
    print(f"REEL: {asset.mp4_path}")
    print(f"  {asset.width}x{asset.height}, {asset.duration_seconds:.1f}s, {asset.size_bytes} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

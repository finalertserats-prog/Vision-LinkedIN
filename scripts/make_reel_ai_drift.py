"""Build a reel of the 'AI drifts to philosophy' post so we can compare an
image-post vs a video reel of the same content. Real agy stills + edge-tts + ffmpeg.
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
    work = Path("prep/reel_ai_drift")
    work.mkdir(parents=True, exist_ok=True)

    script = ReelScript(
        title="AI drifts to philosophy",
        voice="en-US-AndrewNeural",
        scenes=[
            Scene(
                image_prompt="a person scrolling a phone showing deep, heavy philosophical posts, thoughtful cool blue tones",
                narration="I built a system to pick what I post here. Every single post it wrote came out philosophical. Grief, loneliness, deception. Zero tech, the one thing that is actually my edge.",
                on_screen_text="ALL PHILOSOPHY, ZERO TECH",
                duration_seconds=6.0,
            ),
            Scene(
                image_prompt="a person stepping back from a wall of sticky notes, a pattern emerging, quiet aha moment",
                narration="It took me five posts to even notice. Each one looked good on its own. The problem only lived in the pattern across all of them.",
                on_screen_text="THE PATTERN, NOT THE POST",
                duration_seconds=6.0,
            ),
            Scene(
                image_prompt="a compass needle drifting off course toward a glowing comfortable zone, subtle tension",
                narration="These systems drift toward profound sounding topics because that is their comfort zone. It was not broken. It was optimizing. Just for the wrong target.",
                on_screen_text="OPTIMIZING FOR THE WRONG TARGET",
                duration_seconds=6.0,
            ),
            Scene(
                image_prompt="a balanced scale with a deliberate counterweight being set in place, calm resolution at dawn",
                narration="A nicer instruction does nothing against a structural pull. The fix is a counterweight you have to build on purpose.",
                on_screen_text="BUILD THE COUNTERWEIGHT",
                duration_seconds=6.0,
            ),
        ],
    )

    print("1/3 voice-over (edge-tts)...")
    vo = synthesize_voiceover(script, settings=settings, out_dir=str(work))
    print(f"    VO {vo.duration_seconds:.1f}s, scene ends {[round(t, 1) for t in vo.scene_end_times]}")

    print("2/3 anime stills (agy)...")
    client = BrahmastraImageClient(settings, timeout=300.0)
    scenes: list[tuple[str, str, float]] = []
    prev = 0.0
    for i, sc in enumerate(script.scenes):
        img = client.illustrate(sc.image_prompt)
        p = work / f"scene_{i}.png"
        p.write_bytes(img)
        end = vo.scene_end_times[i] if i < len(vo.scene_end_times) else prev + sc.duration_seconds
        scenes.append((str(p), sc.on_screen_text, max(1.5, end - prev)))
        prev = end
        print(f"    scene {i}: {len(img)} bytes")

    print("3/3 assembling (ffmpeg)...")
    asset = assemble_reel(scenes, vo.audio_path, settings=settings, out_path=str(work / "reel.mp4"))
    print(f"REEL: {asset.mp4_path} | {asset.duration_seconds:.1f}s | {asset.size_bytes} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

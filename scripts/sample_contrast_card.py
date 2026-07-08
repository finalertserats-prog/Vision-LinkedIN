"""Generate one real anime contrast-card sample (two agy panels + composite)."""

from __future__ import annotations

from pathlib import Path

from vision.brahmastra.image_client import BrahmastraImageClient
from vision.config import get_settings
from vision.visuals.card_renderer import render_contrast_card

_LEFT_CONCEPT = (
    "a fancy futuristic mansion perched on tall spindly stilts over a deep cracked "
    "chasm, precarious and about to topple, dramatic ominous mood"
)
_RIGHT_CONCEPT = (
    "a solid warm cottage built on deep layered stone bedrock foundations, rooted "
    "and stable, reassuring golden morning light"
)


def main() -> int:
    settings = get_settings()
    client = BrahmastraImageClient(settings)
    print("generating left panel (agy)...")
    left = client.illustrate(_LEFT_CONCEPT)
    print(f"  left ok: {len(left)} bytes")
    print("generating right panel (agy)...")
    right = client.illustrate(_RIGHT_CONCEPT)
    print(f"  right ok: {len(right)} bytes")
    card = render_contrast_card(left, right, "AI FIRST", "FOUNDATIONS FIRST", settings=settings)
    out = Path("prep") / "sample_contrast_card.png"
    out.parent.mkdir(exist_ok=True)
    out.write_bytes(card)
    print(f"SAVED {out} ({len(card)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

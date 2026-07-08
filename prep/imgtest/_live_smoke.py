"""LIVE image smoke: real agy illustrate + deterministic quote card.

Makes a REAL subscription image call via BrahmastraImageClient (agy agent),
validates the returned bytes open in Pillow with sane dimensions, saves it,
then renders a deterministic quote card. Retries the agy call ONCE on
transient failure. Reports honestly if agy still fails — never fakes bytes.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

from PIL import Image

from vision.brahmastra.errors import ImageGenerationError
from vision.brahmastra.image_client import BrahmastraImageClient
from vision.visuals.card_renderer import render_quote_card

OUT_DIR = Path(__file__).resolve().parent
CONCEPT_PATH = OUT_DIR / "live_concept.png"
QUOTE_PATH = OUT_DIR / "live_quote.png"


def _describe(data: bytes) -> tuple[int, tuple[int, int], str]:
    """Open bytes with Pillow; return (size_bytes, (w,h), format)."""
    with Image.open(io.BytesIO(data)) as img:
        img.load()  # force full decode so a truncated file fails here
        return len(data), img.size, (img.format or "UNKNOWN")


def main() -> int:
    report: dict[str, object] = {}

    # --- 1) LIVE agy concept illustration (client has its own 1 retry; we add
    #        one more full retry on top per the task's "retry once" instruction).
    agy_error: str | None = None
    data: bytes | None = None
    client = BrahmastraImageClient()
    for attempt in range(2):
        try:
            data = client.illustrate(
                "a calm abstract flow of light and quiet geometry"
            )
            break
        except ImageGenerationError as exc:
            agy_error = f"{type(exc).__name__}: {exc}"
            print(f"[agy] attempt {attempt + 1}/2 failed: {agy_error}", flush=True)

    if data is not None:
        size, dims, fmt = _describe(data)
        w, h = dims
        sane = w >= 64 and h >= 64  # sane dimensions guard
        CONCEPT_PATH.write_bytes(data)
        report["agy"] = {
            "valid": True,
            "sane_dimensions": sane,
            "bytes": size,
            "width": w,
            "height": h,
            "format": fmt,
            "path": str(CONCEPT_PATH),
        }
        print(
            f"[agy] OK valid image: {size} bytes, {w}x{h}, {fmt} -> {CONCEPT_PATH}",
            flush=True,
        )
    else:
        report["agy"] = {"valid": False, "error": agy_error, "path": None}
        print(f"[agy] FAILED after retry (honest report): {agy_error}", flush=True)

    # --- 2) Deterministic quote card (Pillow, always works) -----------------
    quote_bytes = render_quote_card(
        "The distortion isn't the bug. It might be the whole point."
    )
    size, dims, fmt = _describe(quote_bytes)
    QUOTE_PATH.write_bytes(quote_bytes)
    report["quote"] = {
        "valid": True,
        "bytes": size,
        "width": dims[0],
        "height": dims[1],
        "format": fmt,
        "path": str(QUOTE_PATH),
    }
    print(
        f"[quote] OK: {size} bytes, {dims[0]}x{dims[1]}, {fmt} -> {QUOTE_PATH}",
        flush=True,
    )

    print("REPORT_JSON=" + json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())

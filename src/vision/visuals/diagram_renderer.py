"""Deterministic mermaid-diagram rendering for the tech-post image lane.

WHY this module exists: a genuinely technical council post can carry a small
diagram that AMPLIFIES its idea (how something is built, flows, or is changed by
AI). Per the precision rule (§13.6/D10) anything with words or numbers is
rendered DETERMINISTICALLY, never by diffusion - so the diagram is drawn by the
mermaid CLI (``mmdc``), exactly the tool that produced the owner-approved
clinical-AI architecture diagram. Text labels are therefore SAFE: mermaid lays
them out precisely, they are not hallucinated by an image model.

FAIL-CLOSED (§13.6): rendering shells out to a headless browser via ``mmdc``, so
ANY failure - missing binary, non-zero exit, timeout, empty output - raises
:class:`DiagramRenderError`. The image lane catches it and degrades the draft to
text-only, so a diagram problem NEVER blocks the post.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from vision.config import Settings, get_settings

logger = logging.getLogger(__name__)


class DiagramRenderError(RuntimeError):
    """Rendering a mermaid diagram to PNG failed - the caller degrades to text-only.

    Raised for every failure class (missing ``mmdc``, non-zero exit, timeout,
    empty/absent output) so the single-catch in the image lane can turn any
    diagram problem into a clean text-only degrade (§13.6). The message is kept
    short and non-sensitive (no diagram body, bounded stderr) for safe logging.
    """


def render_mermaid(mermaid: str, settings: Settings | None = None) -> bytes:
    """Render ``mermaid`` source to PNG bytes via the mermaid CLI, or raise.

    Writes the source to a temp ``.mmd``, invokes ``mmdc`` (resolved on PATH so a
    Windows ``mmdc.cmd`` works too) with a white background, and returns the PNG
    bytes. Every failure raises :class:`DiagramRenderError` so the image lane
    degrades to text-only rather than blocking the post (§13.6).

    Args:
        mermaid: The mermaid diagram source (already de-named + validated upstream).
        settings: Config override (defaults to the process singleton) - supplies
            the CLI command (``DIAGRAM_MMDC_CMD``) and render timeout.

    Returns:
        PNG bytes of the rendered diagram.

    Raises:
        DiagramRenderError: on empty source, a missing CLI, a non-zero exit, a
            timeout, or missing/empty output.
    """
    settings = settings or get_settings()
    src = (mermaid or "").strip()
    if not src:
        raise DiagramRenderError("empty mermaid source")

    cmd = shutil.which(settings.diagram_mmdc_cmd)
    if cmd is None:
        raise DiagramRenderError(f"mermaid CLI not found on PATH: {settings.diagram_mmdc_cmd!r}")

    # ALL I/O here (temp-dir creation, write, subprocess launch, read-back) is
    # inside the try so ANY OSError - e.g. a full/read-only temp volume on the VPS -
    # becomes a DiagramRenderError, never a bare OSError that would unwind through
    # attach_council_image / run_council and abort the post. The "diagram never
    # blocks the post" invariant depends on this catch being total.
    try:
        with tempfile.TemporaryDirectory(prefix="vision-diagram-") as tmp:
            in_path = Path(tmp) / "diagram.mmd"
            out_path = Path(tmp) / "diagram.png"
            in_path.write_text(src, encoding="utf-8")
            proc = subprocess.run(
                [
                    cmd,
                    "-i", str(in_path),
                    "-o", str(out_path),
                    "-b", "white",
                    "-t", "default",
                ],
                capture_output=True,
                text=True,
                timeout=max(1, settings.diagram_render_timeout_s),
            )
            if proc.returncode != 0:
                # Bound the stderr so a noisy CLI can't flood the log; no diagram body.
                raise DiagramRenderError(
                    f"mermaid render exited {proc.returncode}: {proc.stderr.strip()[:200]}"
                )
            data = out_path.read_bytes()
            if not data:
                raise DiagramRenderError("mermaid render produced an empty file")
            return data
    except subprocess.TimeoutExpired as exc:
        raise DiagramRenderError("mermaid render timed out") from exc
    except OSError as exc:
        # Temp-dir/write/launch/read failure (disk full, read-only tmp, WinError 193).
        raise DiagramRenderError(
            f"mermaid render I/O failed ({exc.__class__.__name__})"
        ) from exc

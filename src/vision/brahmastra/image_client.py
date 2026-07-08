"""Concept-illustration image client over the CONFIRMED-WORKING agy path (BRD §13.6 / FR-23).

WHY this module exists: the visual lane's *concept-illustration* case needs a
text-free abstract image, generated CLI-only (no API keys, §22). The
CONFIRMED-WORKING path (verified live 2026-07-08) is **agy** (Antigravity /
Gemini) driven as an AGENT under the owner's subscription — NO API key. agy
GENERATES and SAVES a real PNG to a path we hand it; this client then reads the
bytes back. The legacy ``gemini`` CLI is DEAD (IneligibleTierError) and
``gemini_image.sh`` does not work, so agy is THE AI-image path.

PRECISION RULE (BRD §13.6 / D10): anything carrying NUMBERS or WORDS is rendered
DETERMINISTICALLY (Pillow/matplotlib cards) elsewhere — NEVER through agy, which
mangles text/figures. agy is for TEXT-FREE concept illustrations only, so every
prompt is hardened with 'no text, no words, no letters, no logos'.

Critical contract (BRD §13.6 guardrail): image generation MUST *degrade
gracefully* — any failure raises ``ImageGenerationError`` so the caller falls
back to a text-only post and NEVER blocks publishing. This is the deliberate
difference from the text passes, which fail the whole run.
"""

from __future__ import annotations

import io
import logging
import random
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from vision.brahmastra.errors import ImageGenerationError
from vision.config import Settings, get_settings

logger = logging.getLogger(__name__)

# PNG / JPEG magic-number prefixes. WHY: after agy exits we read the saved file
# and confirm it actually begins with an image magic number (not a text error
# message agy might have written) before trusting/returning it.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"

# Mandatory text-free negatives appended to EVERY agy prompt. WHY hard-coded (not
# only config): the precision rule (§13.6/D10) is non-negotiable — agy/diffusion
# must never bake words/numbers/logos into an illustration, regardless of the
# owner-editable style guide. The style guide sets taste; this guarantees safety.
_TEXT_FREE_NEGATIVES = "no text, no words, no letters, no logos"

# The owner is an anime/manga devotee and wants EVERY visual to be hand-drawn art,
# tuned to the elevated/editorial end so it reads as a distinctive personal brand
# on LinkedIn (not kiddie cartoon). A rotating sub-style gives run-to-run variety
# (the "keep changing it" principle) while staying inside the anime/hand-drawn
# family. One is picked per generation and layered onto IMAGE_STYLE_GUIDE.
_ART_STYLES: tuple[str, ...] = (
    "cinematic anime film still, Studio Ghibli-inspired, soft cel shading, "
    "painterly backgrounds",
    "expressive black-and-white manga ink illustration, clean screentone, dynamic linework",
    "delicate graphite pencil sketch, fine cross-hatching, hand-drawn on paper",
    "soft watercolor anime concept art, muted washes, atmospheric light",
    "modern anime key visual, refined line art, dramatic lighting, emotive composition",
)


class BrahmastraImageClient:
    """Adapter that generates concept illustrations by driving agy as an agent.

    ``illustrate`` builds a text-free, style-guided prompt, runs the configured
    ``agy`` binary (``AGY_BIN``) as an agent that SAVES a PNG to a tempfile, then
    reads and validates those bytes — converting a JPEG to PNG if agy saved one.
    On any failure it raises ``ImageGenerationError`` so the caller degrades to a
    text-only post (image never blocks publishing, §13.6).
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        timeout: float = 300.0,
    ) -> None:
        """Wire the image client to config.

        Args:
            settings: Config source (agy binary path + style guide). Defaults to
                the process-wide singleton.
            timeout: Per-attempt bound (seconds) on a hung agy run. WHY ~300s:
                agy generates as an agent (~70s typical) but slows under system load
                (e.g. concurrent council processes) — a 200s ceiling was seen to
                time out and drop the anime image (2026-07-08). 300s gives headroom
                while still guaranteeing the run cannot hang forever. Config-overridable.
        """
        self._settings = settings or get_settings()
        self._timeout = timeout
        # Snapshot the configured binary once. Never logged with a prompt/secret.
        self._agy_bin: str = self._settings.agy_bin

    def illustrate(self, prompt: str, model: str | None = None) -> bytes:
        """Generate a text-free concept illustration and return its PNG bytes.

        Args:
            prompt: The conceptual illustration prompt. It is hardened here with
                the configured style guide + the mandatory text-free negatives, so
                agy can never bake words/numbers/logos into the image (§13.6/D10).
            model: Accepted for interface symmetry with ``BrahmastraClient`` but
                unused — agy is THE (single) AI-image path in this build. Kept so
                callers need not special-case the image client's signature.

        Returns:
            Raw PNG bytes suitable for email embed / LinkedIn upload.

        Raises:
            ImageGenerationError: On ANY failure (timeout, launch error, no file,
                or a non-image file). The caller MUST catch this and degrade to a
                text-only post (BRD §13.6 — image never blocks publishing).
        """
        # ``model`` is intentionally ignored: agy is the only working AI-image
        # lane. Referencing it documents the deliberate no-op for readers.
        _ = model
        styled_prompt = self._build_styled_prompt(prompt)

        # One retry: a single transient agy hiccup (timeout/launch flake) should
        # not cost the image. Attempt indices 0 and 1 → initial + one retry.
        last_error: ImageGenerationError | None = None
        for attempt in range(2):
            try:
                return self._run_agy_once(styled_prompt)
            except ImageGenerationError as exc:
                last_error = exc
                logger.warning(
                    "agy illustration attempt %d/2 failed: %s", attempt + 1, exc
                )

        # Both attempts exhausted → surface the last failure for graceful degrade.
        assert last_error is not None  # loop always ran ≥1 iteration
        raise last_error

    # -- Prompt hardening ---------------------------------------------------

    def _build_styled_prompt(self, prompt: str) -> str:
        """Prepend the style guide + mandatory text-free negatives to ``prompt``.

        WHY prepend the guide: the owner-editable ``IMAGE_STYLE_GUIDE`` (config
        over code, §22.6) sets the house aesthetic, and the fixed negatives make
        the image safe under the precision rule no matter how the concept was
        phrased. Both lead so agy reads the constraints first.
        """
        guide = self._settings.image_style_guide.strip().rstrip(".")
        # Layer a rotating anime/hand-drawn sub-style for run-to-run variety within
        # the owner's art-only house aesthetic (see _ART_STYLES).
        art_style = random.choice(_ART_STYLES)
        concept = prompt.strip()
        return f"{guide}, {art_style}, {_TEXT_FREE_NEGATIVES}. {concept}"

    # -- agy invocation -----------------------------------------------------

    def _run_agy_once(self, styled_prompt: str) -> bytes:
        """Run agy as an agent once: it saves a PNG to a temp path we then read.

        WHY a tempfile: agy (like the confirmed invocation) WRITES the PNG to an
        absolute output path rather than streaming bytes. We create the path,
        instruct agy to save there, run it, then read+validate the bytes — always
        cleaning up the temp artifact regardless of outcome.
        """
        # Create a temp path but close the handle immediately: agy (a separate
        # process) must open+write the file itself on all platforms.
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            out_path = Path(handle.name).resolve()

        # The agent must be allowed to write inside the project working dir; the
        # confirmed invocation passes the absolute CWD via --add-dir.
        abs_cwd = str(Path.cwd().resolve())

        # Exact confirmed-working agent form (verified live 2026-07-08). The -p
        # prompt tells agy to GENERATE, SAVE to the absolute path, and confirm.
        agent_prompt = (
            "Use your image generation capability to create an image: "
            f"{styled_prompt}. Save the generated PNG to {out_path}. "
            "Confirm the file path when done."
        )
        cmd = [
            self._agy_bin,
            "--add-dir",
            abs_cwd,
            "--dangerously-skip-permissions",
            "-p",
            agent_prompt,
        ]

        try:
            self._launch(cmd)
            return self._read_and_normalise(out_path)
        finally:
            # Always remove the temp artifact so failed/successful runs don't leak.
            out_path.unlink(missing_ok=True)

    def _launch(self, cmd: list[str]) -> subprocess.CompletedProcess:
        """Run the agy command with a timeout, normalising launch failures.

        WHY funnel through one method: it is the single external boundary (the
        mock point for tests) and the single place low-level subprocess failures
        become ``ImageGenerationError`` — so the caller only ever catches that
        one type for graceful degradation. Note: the prompt (never a secret) is
        passed as an argv element and is not logged here.
        """
        try:
            # text=True: agy prints status text (path confirmation) to stdout; the
            # image itself arrives via the saved file, not stdout.
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
        except subprocess.TimeoutExpired as exc:
            # Bounded hang → graceful image failure (never blocks publishing).
            raise ImageGenerationError(
                f"agy image generation timed out after {self._timeout}s"
            ) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            # Launch/subprocess errors (agy binary missing, not executable, etc.).
            raise ImageGenerationError(
                f"agy image generation subprocess failed: {exc}"
            ) from exc

    def _read_and_normalise(self, out_path: Path) -> bytes:
        """Read the agy-saved file, validate it is an image, return PNG bytes.

        Accepts a PNG as-is. If agy saved a JPEG, loads it with Pillow and
        re-encodes to PNG so the downstream LinkedIn contract (PNG/JPEG,
        validated separately) always receives a real, normalised image. Anything
        else (missing/empty file, or a text error masquerading as an image) is an
        image failure → ``ImageGenerationError`` for graceful degradation.
        """
        # A missing or empty file means agy produced no image — degrade.
        if not out_path.exists() or out_path.stat().st_size == 0:
            raise ImageGenerationError(
                f"agy produced no output file (expected non-empty {out_path})"
            )

        data = out_path.read_bytes()

        if data.startswith(_PNG_MAGIC):
            # Already the desired format — return the exact saved bytes.
            return data

        if data.startswith(_JPEG_MAGIC):
            # agy saved a JPEG this run: load + convert to PNG. WHY convert (not
            # just pass through): a single, predictable output format keeps the
            # embed/upload path simple and guarantees a lossless PNG downstream.
            return self._jpeg_to_png(data)

        # No recognised image magic → a text error or garbage, not an image.
        raise ImageGenerationError(
            "agy output file is not a recognisable PNG/JPEG image"
        )

    @staticmethod
    def _jpeg_to_png(data: bytes) -> bytes:
        """Convert JPEG bytes to PNG bytes via Pillow, or fail as an image error."""
        try:
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                return buffer.getvalue()
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            # Specific decode failures only (no bare except, §22): a payload Pillow
            # cannot parse is not an image we can safely return.
            raise ImageGenerationError(
                f"agy saved a JPEG that could not be converted to PNG: {exc}"
            ) from exc

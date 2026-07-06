"""Concept-illustration image client over the council CLI (BRD §13.6 / FR-23).

WHY this module exists: the visual lane's *concept-illustration* case needs a
text-free abstract image from a diffusion model, generated CLI-only (no API
keys, §22). This client shells to the council image scripts and returns raw
image bytes for the caller to embed in the approval email / upload to LinkedIn.

Critical contract (BRD §13.6 guardrail): image generation MUST *degrade
gracefully* — a failure raises ``ImageGenerationError`` so the caller falls
back to a text-only post and NEVER blocks publishing. This is the deliberate
difference from the text passes, which fail the whole run.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

from vision.brahmastra.errors import ImageGenerationError
from vision.config import Settings, get_settings

logger = logging.getLogger(__name__)

# PNG / JPEG magic-number prefixes. WHY: when a lane streams image bytes on
# stdout (rather than writing a file), we use these to confirm we actually got
# an image and not a text error message, before returning it as bytes.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"


class BrahmastraImageClient:
    """Adapter that generates concept illustrations via the council image CLIs.

    Routing is model-driven and config-first (``IMAGE_MODEL``): a Gemini-family
    model rides ``gemini_call.sh image`` (which writes a PNG to a path), while a
    Codex / gpt-image model rides ``codex_call.sh image`` (built-in gpt-image).
    Either way ``illustrate`` returns raw bytes or raises ``ImageGenerationError``.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        timeout: float = 180.0,
        bash_executable: str = "bash",
    ) -> None:
        """Wire the image client to config and the bash launcher.

        Args mirror ``BrahmastraClient`` for consistency: ``settings`` supplies
        the council dir + default image model, ``timeout`` bounds a hung
        generation, and ``bash_executable`` is configurable for non-standard
        hosts (config over code, §22.6).
        """
        self._settings = settings or get_settings()
        self._timeout = timeout
        self._bash = bash_executable
        self._council_dir: Path = Path(self._settings.brahmastra_council_dir)

    def illustrate(self, prompt: str, model: str | None = None) -> bytes:
        """Generate a concept illustration and return its raw image bytes.

        Args:
            prompt: The (text-free-styled) illustration prompt. The caller is
                responsible for prepending the fixed style guide (§13.6) — this
                adapter is a dumb transport, mirroring ``BrahmastraClient``.
            model: Override the configured ``IMAGE_MODEL``. WHY configurable:
                image model IDs churn frequently, so they live in config, never
                in code (§13.6 note).

        Returns:
            Raw image bytes (PNG/JPEG) suitable for email embed / LinkedIn upload.

        Raises:
            ImageGenerationError: On any failure. The caller MUST catch this and
                degrade to a text-only post (BRD §13.6 — image never blocks pub).
        """
        chosen = (model or self._settings.image_model or "").lower()
        # Route by model family. Codex/gpt-image models use the codex built-in
        # image path; everything else defaults to the Gemini image path (the
        # primary, file-based generator).
        if "codex" in chosen or "gpt-image" in chosen or "gpt_image" in chosen:
            return self._illustrate_via_codex(prompt, chosen)
        return self._illustrate_via_gemini(prompt)

    # -- Gemini path (file-based) ------------------------------------------

    def _illustrate_via_gemini(self, prompt: str) -> bytes:
        """Generate via ``gemini_call.sh image`` which writes a PNG to a path.

        WHY a temp file: ``gemini_call.sh image`` delegates to
        ``gemini_image.sh "prompt" <output_path> <type>``, which SAVES the
        image to ``output_path`` rather than streaming bytes. We hand it a
        NamedTemporaryFile path, run the CLI, then read the bytes back — cleaning
        up the temp file regardless of outcome.
        """
        script_path = str(self._council_dir / "gemini_call.sh")

        # Create a temp path but close the handle immediately: the CLI (a
        # separate process) needs to open+write the file itself on all platforms.
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            out_path = Path(handle.name)

        try:
            # Positional layout consumed by gemini_call.sh → gemini_image.sh:
            #   $1 prompt  $2 mode(image)  $3 output_path  $4 type
            self._run_image_subprocess(
                [self._bash, script_path, prompt, "image", str(out_path), "illustration"]
            )

            # Success is defined by a non-empty file existing on disk. An empty
            # or missing file means the generator failed — degrade gracefully.
            if out_path.exists() and out_path.stat().st_size > 0:
                return out_path.read_bytes()

            raise ImageGenerationError(
                "Gemini image lane produced no output file "
                f"(expected non-empty {out_path})"
            )
        finally:
            # Always remove the temp artifact so failed runs don't leak files.
            out_path.unlink(missing_ok=True)

    # -- Codex path (stdout-based) -----------------------------------------

    def _illustrate_via_codex(self, prompt: str, model: str) -> bytes:
        """Generate via ``codex_call.sh image`` (built-in gpt-image).

        WHY capture bytes: the codex image mode streams to stdout rather than a
        file. We capture raw bytes and only accept output that begins with a
        known image magic number — anything else (a text error/echo) is treated
        as a failure and degraded gracefully.
        """
        script_path = str(self._council_dir / "codex_call.sh")

        completed = self._run_image_subprocess(
            [self._bash, script_path, prompt, "image"], capture_bytes=True
        )
        # ``stdout`` here is bytes because capture_bytes routes text=False.
        data: bytes = completed.stdout or b""

        # Validate we actually received image bytes, not an error message.
        if data.startswith(_PNG_MAGIC) or data.startswith(_JPEG_MAGIC):
            return data

        raise ImageGenerationError(
            f"Codex image lane ({model!r}) did not return recognisable image bytes"
        )

    # -- Shared subprocess runner ------------------------------------------

    def _run_image_subprocess(
        self, cmd: list[str], *, capture_bytes: bool = False
    ) -> subprocess.CompletedProcess:
        """Run an image CLI command with timeout, normalising failures.

        WHY funnel through one method: it is the single external boundary (mock
        point for tests) and the single place we convert low-level subprocess
        failures into ``ImageGenerationError`` so the caller only ever needs to
        catch that one type for graceful degradation.

        ``capture_bytes`` selects binary stdout (codex streams raw image bytes)
        vs. text (gemini writes a file and prints only status text).
        """
        try:
            # text=not capture_bytes → binary stdout when we expect image bytes.
            return subprocess.run(
                cmd,
                capture_output=True,
                text=not capture_bytes,
                timeout=self._timeout,
            )
        except subprocess.TimeoutExpired as exc:
            # Bounded hang → graceful image failure (never blocks publishing).
            raise ImageGenerationError(
                f"Image generation timed out after {self._timeout}s"
            ) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            # Launch/subprocess errors (bash missing, script unreadable, etc.).
            raise ImageGenerationError(
                f"Image generation subprocess failed: {exc}"
            ) from exc

"""The three headless AI voices of the Brahmastra Council (BRD §5 / §13.1).

WHY this module exists: the council deliberates by asking three DIFFERENT AI
lanes the same question and letting them genuinely disagree. Each lane runs
*fully headless, CLI-only, no API keys* (§22 / D6): Gemini via
``~/.claude/council/agy_call.sh``, Codex via ``~/.claude/council/codex_call.sh``,
and Claude via the ``claude -p`` CLI. This file is the single, stable transport
seam — a uniform :func:`Voices.ask` returning the voice's raw text — so the
deliberation / compose logic upstream never touches ``subprocess`` directly.

Design intent, mapped to the proven prototype (``scripts/council.py``) and the
conventions (§22):

* **Positional-arg invocation (the key safety property).** Every voice is
  invoked through ``bash -c <command> _ <prompt>`` so the prompt lands in bash's
  ``$1`` — it is NEVER interpolated into the command string. That means a prompt
  full of quotes, newlines or shell metacharacters can neither break the command
  nor inject shell (verbatim from the prototype's ``ask()``).
* **Fail-soft per voice.** A single dead/timed-out voice returns ``""`` rather
  than raising, so the council degrades to the surviving voices instead of the
  whole run crashing. The *fail-closed* decision (too few voices) is made one
  layer up, in :mod:`vision.council.deliberate` / :mod:`vision.council.engine`.
* **Clean UTF-8 capture.** ``capture_output`` bytes are decoded with
  ``errors="ignore"`` and NULs/control noise some CLIs emit are stripped, so the
  answer is always clean text.
* **Config over code.** The council directory and the Claude binary are read from
  :class:`~vision.config.Settings` (defaults ``~/.claude/council`` and
  ``claude``) and — learning from the ``client.py`` council-dir bug — the ``~``
  is expanded and script paths are handed to bash as POSIX (forward-slash) paths
  so MSYS/Git-bash on Windows finds them.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from vision.config import Settings, get_settings

logger = logging.getLogger(__name__)

# The three voice identities. Kept as module constants so callers reference a
# stable vocabulary and the deliberation loop can iterate them deterministically.
GEMINI = "Gemini"
CODEX = "Codex"
CLAUDE = "Claude"
#: Canonical council order — every round iterates voices in THIS order so the
#: transcript and per-round dicts are deterministic across runs.
VOICE_ORDER: tuple[str, ...] = (GEMINI, CODEX, CLAUDE)

# Default per-invocation wall-clock ceiling (seconds). Bounds a hung CLI so one
# stuck model can't stall the whole council (mirrors the prototype's 180s).
_DEFAULT_TIMEOUT = 180.0

# The extra positional args the shell scripts expect after the prompt. These are
# verbatim from the proven prototype's _VOICE_CMD ("default 1 25" = mode,
# <script-specific flags>) — do NOT reword; they are part of the proven path.
_SCRIPT_TRAILING_ARGS = ("default", "1", "25")


class Voices:
    """The three headless voices with a uniform ``ask(voice, prompt) -> str``.

    Instances are cheap and stateless apart from their resolved config, so the
    deliberation/compose layers construct one per run (or share one). External
    dependencies (``subprocess``, the ``bash`` launcher) are injected/overridable
    so unit tests can MOCK them and NEVER call a real model.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
        bash_executable: str = "bash",
    ) -> None:
        """Resolve config and injectable dependencies once.

        Args:
            settings: Config source (council dir + Claude binary). Falls back to
                the process-wide cached singleton so callers need not pass it.
            timeout: Per-invocation wall-clock ceiling in seconds (NFR-07) — a
                stuck model returns ``""`` (fail-soft) rather than hanging.
            bash_executable: The bash launcher. Configurable because Windows hosts
                may expose it under a non-default name/path (§22.6).
        """
        self._settings = settings or get_settings()
        self._timeout = timeout
        self._bash = bash_executable
        # Expand '~' HERE (Python does not, unlike bash) and keep as a Path so we
        # can emit POSIX script paths. This is the council-dir bug fix from
        # client.py: an unexpanded '~/...' makes bash receive a literal '~' and
        # return empty output.
        self._council_dir: Path = Path(
            os.path.expanduser(self._settings.brahmastra_council_dir)
        )

    def _command_for(self, voice: str) -> str:
        """Return the ``bash -c`` command string for ``voice``.

        The command references bash's ``$1`` for the prompt — the prompt is
        passed as a POSITIONAL arg by :meth:`ask`, never interpolated here — so
        prompt content can't break or inject the command. Script paths are POSIX
        (forward-slash) so Git/MSYS bash on Windows resolves them (the client.py
        as_posix() lesson).

        Raises:
            KeyError: for an unknown voice name — a caller bug that should fail
                loudly rather than silently mis-route.
        """
        trailing = " ".join(_SCRIPT_TRAILING_ARGS)
        if voice == GEMINI:
            script = (self._council_dir / "agy_call.sh").as_posix()
            return f'"{script}" "$1" {trailing}'
        if voice == CODEX:
            script = (self._council_dir / "codex_call.sh").as_posix()
            return f'"{script}" "$1" {trailing}'
        if voice == CLAUDE:
            # The Claude voice is the local CLI binary (config-driven name), run
            # in headless print mode. The binary name is quoted defensively.
            claude_bin = self._settings.council_claude_bin
            return f'"{claude_bin}" -p "$1"'
        raise KeyError(f"Unknown council voice {voice!r}; expected one of {VOICE_ORDER}")

    def ask(self, voice: str, prompt: str) -> str:
        """Return ``voice``'s raw text answer, or ``""`` on failure (fail-soft).

        Invokes the voice's CLI through ``bash -c <command> _ <prompt>`` so the
        prompt is bash's ``$1`` (the ``_`` occupies ``$0``). Any subprocess/OS
        error or timeout is logged (class + voice only, NEVER the prompt, which
        could contain sensitive drafting context) and reduced to ``""`` so a
        single dead voice degrades the council instead of crashing it — the
        fail-CLOSED "too few voices" decision is made upstream.

        Args:
            voice: One of :data:`VOICE_ORDER`.
            prompt: The full question for this voice; may contain any quotes /
                newlines / shell metacharacters — they are safe as ``$1``.

        Returns:
            The voice's cleaned UTF-8 answer, or ``""`` if it failed to produce
            usable output.
        """
        try:
            command = self._command_for(voice)
        except KeyError:
            # An unknown voice is a programming error, not a transient failure;
            # log it and fail soft to '' so a mis-wired caller degrades safely.
            logger.error("Refusing to call unknown council voice %r.", voice)
            return ""

        try:
            completed = subprocess.run(
                # '_' is a throwaway $0; the prompt is $1 — never string-formatted
                # into the command, so it cannot break or inject the shell.
                [self._bash, "-c", command, "_", prompt],
                capture_output=True,
                timeout=self._timeout,
            )
        except subprocess.TimeoutExpired:
            # A bounded hang — one stuck model must not stall the council.
            logger.warning("Council voice %s timed out after %ss.", voice, self._timeout)
            return ""
        except (OSError, subprocess.SubprocessError) as exc:
            # Launch failure (bash missing, script unreadable) or other subprocess
            # error. Log the CLASS only — never the prompt — so nothing sensitive
            # reaches the logs (§22 never-log-secrets).
            logger.warning(
                "Council voice %s call failed: %s", voice, exc.__class__.__name__
            )
            return ""

        # Decode with errors='ignore' and strip NUL/whitespace noise some CLIs
        # emit around the real answer, yielding clean text either way.
        text = completed.stdout.decode("utf-8", "ignore").replace("\x00", "").strip()
        if not text:
            logger.warning("Council voice %s returned empty output.", voice)
        return text

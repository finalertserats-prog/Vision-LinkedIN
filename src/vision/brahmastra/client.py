"""Thin adapter over the local Brahmastra council CLI scripts (BRD §13.0/§13.1).

WHY this module exists: VISION's synthesis engine is the owner's existing
God-Mode-Brahmastra ensemble, invoked *CLI-only, no API keys* (BRD §22 + D6
CLI path). This adapter exposes a stable, deterministic internal contract —
``generate`` / ``critique`` / ``verify``, each returning a validated ``dict`` —
so the rest of VISION depends only on this file. If Brahmastra's interface
changes, this is the single file to update (§21 risk mitigation).

Design intent, mapped to the BRD:
* Three passes route to *different* lanes (Model A/B/C) for genuine
  cross-checking (§13.1); the lane per pass is config-driven
  (``MODEL_GENERATE/CRITIQUE/VERIFY``), never hard-coded (§22.6 config-over-code).
* Every pass returns *strict JSON*; a non-JSON / empty response raises
  ``BrahmastraError`` so the pipeline fails loudly, not silently (§22.5/§22.9).
* Timeout + one retry guard against transient CLI/model hiccups (NFR-07).
* The council directory is configurable via ``BRAHMASTRA_COUNCIL_DIR`` so the
  script location is never baked into code.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable
from pathlib import Path

from vision.brahmastra.errors import BrahmastraError
from vision.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Sentinel distinguishing "the whole blob is not valid JSON" from a legitimately
# parsed ``None`` (JSON ``null``). WHY a unique object rather than ``None``: a
# literal ``null`` response IS drift we must reject, so it has to travel down the
# wrong-shape branch — not be mistaken for "no JSON, try the prose fallback".
_NO_JSON: object = object()

# --- Lane → (script, cli-arg) mapping --------------------------------------
# WHY a static table: each council script is invoked as
# ``bash <script> "prompt" [mode]`` (see ~/.claude/council/*.sh). The second
# positional arg means different things per script — a *mode* for
# gemini/codex, a *subbing_for* tag for local_call.sh — so we bind the exact
# arg each lane needs here. Keeping this in one place makes "which script +
# which mode per lane" auditable and testable (§22 quality bar).
#
# "default" mode on gemini/codex returns the model's plain-text answer (no
# file writes / yolo), which is exactly what we want: a JSON string on stdout.
# The "claude" lane rides local_call.sh with subbing_for="claude"; a callable
# may be injected instead (see ``claude_callable``) when a first-class Claude
# path is available.
_LANE_CONFIG: dict[str, tuple[str, str]] = {
    "gemini": ("gemini_call.sh", "default"),
    "codex": ("codex_call.sh", "default"),
    "claude": ("local_call.sh", "claude"),
    "local": ("local_call.sh", "solo"),
}


def _strip_code_fences(text: str) -> str:
    """Remove Markdown code-fence wrappers around a model's JSON payload.

    WHY: models routinely wrap JSON in ```json ... ``` or ``` ... ``` fences.
    Stripping them first makes the downstream brace-scan robust to that common
    formatting without a brittle regex on the whole document.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    # Drop the opening fence line (which may carry a language tag like ```json)
    # and any trailing closing fence, returning only the inner content.
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _extract_json_object(text: str) -> str:
    """Return the first balanced ``{...}`` object found in ``text``.

    WHY a hand-rolled brace scanner as a FALLBACK (``_parse_json`` tries a
    whole-blob ``json.loads`` first): model output sometimes wraps the object in
    prose before/after the JSON ("Here you go:\n{...}\nHope that helps"). When
    the whole blob is therefore not itself valid JSON, we locate the first ``{``
    and walk forward tracking brace depth — while ignoring braces inside string
    literals and escaped characters — to find its true matching ``}``. This
    tolerates surrounding prose yet still fails loudly (empty return) when no
    object exists, upholding the deterministic-contract rule (§22.5). Callers
    must only reach here for object-shaped payloads: a leading ``[`` is rejected
    upstream so this scanner never mines an object out of an array.
    """
    start = text.find("{")
    if start == -1:
        return ""

    depth = 0  # current brace nesting level
    in_string = False  # are we inside a JSON string literal?
    escaped = False  # was the previous char a backslash inside a string?

    for index in range(start, len(text)):
        char = text[index]

        # Inside a string, only an unescaped quote can end it; braces are literal.
        if in_string:
            if escaped:
                escaped = False  # this char is consumed by the escape
            elif char == "\\":
                escaped = True  # next char is escaped, ignore its meaning
            elif char == '"':
                in_string = False  # closing quote
            continue

        # Outside a string: track structure.
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            # Depth returning to zero closes the first top-level object.
            if depth == 0:
                return text[start : index + 1]

    # Unbalanced braces → no complete object.
    return ""


class BrahmastraClient:
    """Adapter that shells out to the council CLI scripts and returns dicts.

    Each public method (``generate``/``critique``/``verify``) selects a *lane*
    (``gemini`` | ``codex`` | ``claude`` | ``local``), invokes the matching
    council script via ``bash``, and parses the model's JSON output into a
    ``dict``. Non-JSON output raises ``BrahmastraError`` (fail loudly, §22.5).
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        claude_callable: Callable[[str], str] | None = None,
        timeout: float = 180.0,
        bash_executable: str = "bash",
    ) -> None:
        """Wire the adapter to config and injectable dependencies.

        Args:
            settings: Config source (council dir + per-pass lane defaults). Falls
                back to the process-wide singleton so callers need not pass it.
            claude_callable: Optional first-class Claude entrypoint. WHY: the
                "claude" lane maps to ``local_call.sh`` by default, but a host
                that already has Claude in-process can inject a callable so the
                claude pass runs natively instead of shelling out. Injection also
                keeps that path unit-testable without a subprocess.
            timeout: Per-invocation wall-clock ceiling (seconds) — bounds a hung
                CLI so a stuck model can't stall the daily run (NFR-07/NFR-09).
            bash_executable: The bash launcher; configurable because Windows
                hosts may expose it under a non-default name/path (config over
                code, §22.6).
        """
        # Resolve settings once; the singleton is cached so this is cheap.
        self._settings = settings or get_settings()
        self._claude_callable = claude_callable
        self._timeout = timeout
        self._bash = bash_executable
        # Council directory is config-driven (BRAHMASTRA_COUNCIL_DIR); resolve to
        # a concrete Path so script paths are unambiguous across cwd changes.
        self._council_dir: Path = Path(self._settings.brahmastra_council_dir)

    # -- Public synthesis contract -----------------------------------------
    # The three passes differ ONLY in their default lane (Model A/B/C, §13.1);
    # the RAFT instructions that distinguish "draft" vs "critique" vs "verify"
    # live in the prompt the caller supplies (§13.4), keeping this adapter a
    # dumb, stable transport.

    def generate(self, prompt: str, lane: str | None = None) -> dict:
        """Run the *generate* pass (Model A) and return its parsed JSON dict."""
        return self._call(prompt, lane or self._settings.model_generate)

    def critique(self, prompt: str, lane: str | None = None) -> dict:
        """Run the *critique/edit* pass (Model B) and return its parsed dict."""
        return self._call(prompt, lane or self._settings.model_critique)

    def verify(self, prompt: str, lane: str | None = None) -> dict:
        """Run the *verify* pass (Model C) and return its parsed dict."""
        return self._call(prompt, lane or self._settings.model_verify)

    # -- Internals ----------------------------------------------------------

    def _call(self, prompt: str, lane: str) -> dict:
        """Invoke ``lane`` for ``prompt`` and parse the result into a dict.

        Orchestrates the two halves of the contract: (1) get raw text from the
        CLI with timeout+retry, (2) extract & validate strict JSON. Any breach
        surfaces as ``BrahmastraError`` so the pipeline fails closed.
        """
        raw = self._invoke_cli(prompt, lane)
        return self._parse_json(raw, lane)

    def _resolve_lane(self, lane: str) -> tuple[str, str]:
        """Map a lane name to its (script-path, cli-arg) pair.

        Raises ``BrahmastraError`` on an unknown lane so a config typo fails
        loudly at call time rather than silently mis-routing (§22.9 fail-closed).
        """
        config = _LANE_CONFIG.get(lane)
        if config is None:
            raise BrahmastraError(
                f"Unknown Brahmastra lane {lane!r}; "
                f"expected one of {sorted(_LANE_CONFIG)}"
            )
        script_name, cli_arg = config
        return str(self._council_dir / script_name), cli_arg

    def _invoke_cli(self, prompt: str, lane: str) -> str:
        """Return raw stdout from the lane's CLI, with timeout + one retry.

        WHY timeout+retry (NFR-07): council scripts wrap remote models that can
        transiently time out or return empty on a cold backend. One retry
        recovers the common transient case without masking a persistent failure
        — after the retry we raise ``BrahmastraError`` so the run fails loudly.

        Empty output is treated as a failure (not a valid "no answer") because
        every pass MUST yield a JSON contract; silence is a breach.
        """
        script_path, cli_arg = self._resolve_lane(lane)

        last_error = "unknown error"
        # Two total attempts: the initial call plus exactly one retry.
        for attempt in range(1, 3):
            try:
                # The "claude" lane may be served by an injected in-process
                # callable instead of a subprocess — preferred when available.
                if lane == "claude" and self._claude_callable is not None:
                    output = self._claude_callable(prompt)
                else:
                    output = self._run_subprocess(script_path, prompt, cli_arg)

                # Non-empty stdout is our only success signal: the scripts can
                # exit non-zero on benign warnings yet still emit a full answer,
                # so we gate on content, not just returncode.
                if output and output.strip():
                    return output

                last_error = "empty output"
                logger.warning(
                    "brahmastra lane returned empty output",
                    extra={"lane": lane, "attempt": attempt},
                )

            except subprocess.TimeoutExpired:
                # Bounded hang — record and let the loop retry once.
                last_error = f"timed out after {self._timeout}s"
                logger.warning(
                    "brahmastra lane timed out",
                    extra={"lane": lane, "attempt": attempt},
                )
            except (OSError, subprocess.SubprocessError) as exc:
                # Launch failures (bash missing, script unreadable) or other
                # subprocess errors — specific exceptions only (no bare except).
                last_error = str(exc)
                logger.warning(
                    "brahmastra lane subprocess error",
                    extra={"lane": lane, "attempt": attempt, "error": str(exc)},
                )
            except Exception as exc:  # noqa: BLE001 - injected callable is arbitrary
                # WHY catch broadly HERE: the injected ``claude_callable`` is
                # caller-supplied code whose failure modes we can't enumerate.
                # We normalise any failure into the adapter's contract so callers
                # fail loudly via ``BrahmastraError`` (§22.9), never a stray type.
                last_error = f"claude callable failed: {exc}"
                logger.warning(
                    "brahmastra claude callable error",
                    extra={"lane": lane, "attempt": attempt, "error": str(exc)},
                )

        # Both attempts exhausted without usable output → fail loudly.
        raise BrahmastraError(
            f"Brahmastra {lane!r} lane produced no usable output ({last_error})"
        )

    def _run_subprocess(self, script_path: str, prompt: str, cli_arg: str) -> str:
        """Execute ``bash <script> <prompt> <cli_arg>`` and return stdout text.

        Kept as a small, single-purpose method so tests can assert the exact
        command (script + mode) and so ``subprocess.run`` is the one place the
        external boundary is crossed (mock-here for hermetic tests, §22 tests).
        """
        # ``capture_output`` + ``text`` give us decoded stdout/stderr; ``timeout``
        # enforces the wall-clock ceiling. We deliberately do NOT set check=True
        # because a non-zero exit with real stdout is still a usable answer.
        completed = subprocess.run(
            [self._bash, script_path, prompt, cli_arg],
            capture_output=True,
            text=True,
            timeout=self._timeout,
        )
        return completed.stdout or ""

    def _parse_json(self, raw: str, lane: str) -> dict:
        """Extract, decode, and validate a JSON *object* from raw model output.

        Extraction order (per the fail-loudly contract, §22.5):

          1. Strip code fences, then ``json.loads`` the WHOLE blob first. If it
             is a ``dict`` → accept. If it parses to any OTHER top-level value
             (list/scalar) → that is drift and we raise. WHY first: a plain array
             like ``[{"ok": true}]`` is valid JSON whose first ``{`` sits inside
             the array. The old brace-scanner located that inner ``{`` and
             silently unwrapped it, accepting an array as if it were the object —
             the exact bug this closes. Whole-blob parsing rejects the array.
          2. Only if the whole blob is NOT itself valid JSON (i.e. it is prose
             WRAPPING a single object, e.g. "Here you go:\\n{...}") do we fall
             back to brace-scanning — and even then we refuse a payload that
             starts with ``[`` (an array), and require the scanned result to be a
             ``dict``.

        Any failure raises ``BrahmastraError`` with the offending (truncated)
        text so the drift is diagnosable without dumping unbounded output.
        """
        stripped = _strip_code_fences(raw)

        # 1. Whole-blob parse: the strict, unambiguous path. A clean JSON array
        #    or scalar is caught HERE as drift instead of being mined for an
        #    inner object.
        try:
            whole = json.loads(stripped)
        except json.JSONDecodeError:
            whole = _NO_JSON  # not clean JSON → maybe prose-wrapped; try step 2
        if whole is not _NO_JSON:
            if isinstance(whole, dict):
                return whole
            # Valid JSON but the WRONG shape (list/scalar/null) — refuse it.
            raise BrahmastraError(
                f"Brahmastra {lane!r} lane returned JSON of type "
                f"{type(whole).__name__}, expected object/dict"
            )

        # 2. Prose-wrapped fallback. Refuse a leading '[' outright: that is an
        #    array (possibly malformed) and we must never dig an object out of
        #    it. Only an object-shaped or prose-prefixed payload is scanned.
        if stripped.startswith("["):
            raise BrahmastraError(
                f"Brahmastra {lane!r} lane returned a JSON array, expected "
                f"object/dict; got: {raw[:200]!r}"
            )

        candidate = _extract_json_object(stripped)
        if not candidate:
            raise BrahmastraError(
                f"Brahmastra {lane!r} lane returned no JSON object; "
                f"got: {raw[:200]!r}"
            )

        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            # Malformed JSON is a hard contract breach — surface it, don't guess.
            raise BrahmastraError(
                f"Brahmastra {lane!r} lane returned invalid JSON: {exc}; "
                f"candidate: {candidate[:200]!r}"
            ) from exc

        # Even a prose-mined candidate must be an object — anything else is drift.
        if not isinstance(parsed, dict):
            raise BrahmastraError(
                f"Brahmastra {lane!r} lane returned JSON of type "
                f"{type(parsed).__name__}, expected object/dict"
            )

        return parsed

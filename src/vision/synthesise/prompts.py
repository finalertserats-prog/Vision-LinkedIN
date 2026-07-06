"""Render each synthesis pass prompt from staged config (BRD §13.4, §22.6).

WHY this module exists: BRD §22 mandates *config over code* — the RAFT prompt
contracts and the owner's voice profile are editable files in ``prep/``
(``raft_prompts.md`` + ``voice_profile.yaml``), never hard-coded strings. This
module loads those files ONCE and composes the exact prompt string handed to the
Brahmastra CLI for each pass, injecting the runtime inputs (focus, items, prior
pass output) while preserving the Role-Action-Format-Target structure verbatim.

Design intent:
  * The RAFT section text is embedded UNCHANGED so the deterministic JSON
    contract the downstream schema validates is never paraphrased away (§13.4).
  * Runtime data (focus/items/prior-draft) is appended in a clearly delimited,
    JSON-serialised block so the model sees structured, unambiguous inputs.
  * Paths are overridable via env vars so a deployment can relocate the config
    without touching code (config over code); the default resolves to ``prep/``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml

# The four RAFT passes we parse out of ``raft_prompts.md``. The markdown headings
# read "## Pass N — GENERATE ...", so each key is matched case-insensitively
# against the heading text.
_PASS_KEYS: tuple[str, ...] = ("GENERATE", "CRITIQUE", "VERIFY", "IMAGE")

# Repo ``prep/`` is three parents up from this file (synthesise -> vision -> src
# -> repo root), computed rather than hard-coded so it survives a checkout move.
_DEFAULT_PREP: Path = Path(__file__).resolve().parents[3] / "prep"


class PromptContractError(ValueError):
    """Raised when the staged prompt config is missing a required RAFT pass.

    WHY a specific ``ValueError`` subclass: a missing pass section is a config
    defect that must fail loudly at load time (§22.9), and callers can catch it
    distinctly from unrelated value errors.
    """


def _parse_sections(markdown: str) -> dict[str, str]:
    """Split ``raft_prompts.md`` into ``{PASS_KEY: section_text}``.

    WHY a heading scan rather than a regex over the whole doc: the file is
    authored as ``## Pass N — NAME`` blocks; walking line-by-line and flushing on
    each ``## Pass`` heading keeps each section's inner markdown (including its
    fenced JSON ``Format`` block) intact and order-independent.
    """
    sections: dict[str, str] = {}
    current_key: str | None = None
    buffer: list[str] = []

    for line in markdown.splitlines():
        if line.startswith("## Pass"):
            # Flush the section we were accumulating before starting a new one.
            if current_key is not None:
                sections[current_key] = "\n".join(buffer).strip()
            buffer = [line]
            # Match against the heading TITLE only (before the "(lane: ...)"
            # parenthetical): Pass 4's lane note reads "MODEL_CRITIQUE OR
            # MODEL_VERIFY", which would otherwise mis-bind the IMAGE block to
            # the CRITIQUE key. Splitting on "(" isolates the true pass name.
            heading = line.split("(", 1)[0].upper()
            current_key = next((key for key in _PASS_KEYS if key in heading), None)
        elif current_key is not None:
            # Only accumulate lines once we're inside a recognised pass block;
            # the file's intro prose (before Pass 1) is intentionally ignored.
            buffer.append(line)

    if current_key is not None:
        sections[current_key] = "\n".join(buffer).strip()
    return sections


class PromptLibrary:
    """Loads the RAFT contracts + voice profile and renders per-pass prompts.

    One instance is built per run (cheap: two small file reads) and reused across
    the three passes so the config is read exactly once.
    """

    def __init__(self, *, prompts_path: Path, voice_path: Path) -> None:
        """Load and validate the staged prompt config.

        Args:
            prompts_path: Path to ``raft_prompts.md`` (the RAFT contracts).
            voice_path: Path to ``voice_profile.yaml`` (tone/dos/donts/bans).

        Raises:
            FileNotFoundError: if either config file is absent (fail loudly —
                the pipeline cannot synthesise without its contracts).
            PromptContractError: if a required RAFT pass section is missing.
        """
        # ``read_text`` raises FileNotFoundError on a missing file — exactly the
        # loud failure we want; we do not paper over absent config.
        self._sections = _parse_sections(prompts_path.read_text(encoding="utf-8"))
        self._voice_raw = voice_path.read_text(encoding="utf-8")
        # ``safe_load`` never executes arbitrary tags — safe for a config file.
        self._voice: dict[str, Any] = yaml.safe_load(self._voice_raw) or {}

        # Every pass must be present; a missing contract is a hard config defect.
        missing = [key for key in _PASS_KEYS if key not in self._sections]
        if missing:
            raise PromptContractError(
                f"raft_prompts.md is missing required pass section(s): {missing}"
            )

    @classmethod
    def default(cls) -> "PromptLibrary":
        """Build from the staged ``prep/`` files, honouring env-var overrides.

        WHY env overrides: a deployment can point VISION at a different prompt or
        voice file without a code change (config over code, §22.6). Absent the
        overrides, the repo's ``prep/`` files are used.
        """
        prompts_path = Path(
            os.environ.get("VISION_RAFT_PROMPTS_PATH", _DEFAULT_PREP / "raft_prompts.md")
        )
        voice_path = Path(
            os.environ.get("VISION_VOICE_PROFILE_PATH", _DEFAULT_PREP / "voice_profile.yaml")
        )
        return cls(prompts_path=prompts_path, voice_path=voice_path)

    @property
    def voice(self) -> dict[str, Any]:
        """The parsed voice profile (banned phrases, length bounds, hashtags...)."""
        return self._voice

    # -- Per-pass renderers --------------------------------------------------

    def generate_prompt(self, focus: str, items: list[dict[str, Any]]) -> str:
        """Render the Pass-1 GENERATE prompt for ``focus`` over ``items``."""
        return self._compose("GENERATE", focus, items)

    def critique_prompt(
        self, focus: str, items: list[dict[str, Any]], draft: dict[str, Any]
    ) -> str:
        """Render the Pass-2 CRITIQUE prompt, supplying the draft to revise."""
        return self._compose(
            "CRITIQUE",
            focus,
            items,
            extra_blocks=[("draft to revise (JSON)", draft)],
        )

    def verify_prompt(
        self, focus: str, items: list[dict[str, Any]], revised: dict[str, Any]
    ) -> str:
        """Render the Pass-3 VERIFY prompt, supplying the revised draft to check."""
        return self._compose(
            "VERIFY",
            focus,
            items,
            extra_blocks=[("revised draft to verify (JSON)", revised)],
        )

    def image_prompt(
        self, final_post: dict[str, Any], grounded_claims: list[dict[str, Any]]
    ) -> str:
        """Render the Pass-4 IMAGE-DECISION prompt over the final post.

        This pass needs only the finished post and the grounded claims (so any
        card datapoint can be traced to a source), not the full item feed.
        """
        raft = self._sections["IMAGE"]
        parts = [
            raft,
            "",
            "--- RUNTIME INPUTS (obey the RAFT contract above; return ONLY the JSON) ---",
            "final post (JSON):",
            json.dumps(final_post, indent=2, ensure_ascii=False, default=str),
            "",
            "grounded claims (JSON):",
            json.dumps(grounded_claims, indent=2, ensure_ascii=False, default=str),
        ]
        return "\n".join(parts)

    # -- Internals -----------------------------------------------------------

    def _compose(
        self,
        pass_key: str,
        focus: str,
        items: list[dict[str, Any]],
        *,
        extra_blocks: list[tuple[str, Any]] | None = None,
    ) -> str:
        """Assemble ``<RAFT section> + <runtime inputs>`` for one pass.

        The RAFT section is embedded verbatim to preserve the deterministic
        contract; the runtime block is JSON so the model receives unambiguous,
        machine-shaped inputs. ``default=str`` lets datetimes/UUIDs serialise.
        """
        raft = self._sections[pass_key]
        parts = [
            raft,
            "",
            "--- RUNTIME INPUTS (obey the RAFT contract above; return ONLY the JSON) ---",
            f"focus: {focus}",
            "",
            "voice_profile (YAML):",
            self._voice_raw.strip(),
            "",
            "items (JSON):",
            json.dumps(items, indent=2, ensure_ascii=False, default=str),
        ]
        for label, payload in extra_blocks or []:
            parts.extend(
                ["", f"{label}:", json.dumps(payload, indent=2, ensure_ascii=False, default=str)]
            )
        return "\n".join(parts)

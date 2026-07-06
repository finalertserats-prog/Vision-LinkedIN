"""Quality report + grounding gate for a synthesised draft (BRD §14.4, §13.5).

WHY this module exists: after the three passes run, VISION must produce a single
machine-readable ``quality_report`` (stored as JSON on ``drafts.quality_report``)
and decide auto-eligibility. Keeping this as PURE functions over the validated
pass outputs (no I/O, no model calls) makes the scoring deterministic and trive
to unit-test — the same inputs always yield the same report.

The report keys are fixed by BRD §14.4:
    char_count, has_hook, grounding_pct, unsupported_claims,
    tone_flags, compliance_flags, hashtags, confidence
"""

from __future__ import annotations

from typing import Any

from vision.config import Settings
from vision.synthesise.schemas import CritiqueOut, VerifyOut


def assemble_post_text(
    hook: str, body: str, takeaway: str, hashtags: list[str]
) -> str:
    """Join the verified post parts into the final publishable text.

    WHY here (not in the pipeline): char_count and the length compliance check
    both operate on the SAME assembled string, so assembling it once in the
    quality layer keeps those two derived values consistent by construction.
    Hashtags are appended on their own line, mirroring how they read on LinkedIn.
    """
    # Skip any empty part so we never emit stray blank paragraphs.
    blocks = [part.strip() for part in (hook, body, takeaway) if part and part.strip()]
    text = "\n\n".join(blocks)
    if hashtags:
        text = f"{text}\n\n{' '.join(hashtags)}"
    return text


def find_banned_phrases(text: str, banned_phrases: list[str]) -> list[str]:
    """Return the voice-profile banned phrases present in ``text`` (case-insensitive).

    WHY case-insensitive substring: the banned list targets clickbait/hype
    wording ("game changer", "revolutionary") regardless of capitalisation. The
    result is de-duplicated while preserving the config's ordering so the tone
    flags read predictably.
    """
    lowered = text.lower()
    flagged: list[str] = []
    for phrase in banned_phrases:
        # A phrase can appear in the banned list only once, but guard anyway so a
        # duplicated config entry never double-reports.
        if phrase.lower() in lowered and phrase not in flagged:
            flagged.append(phrase)
    return flagged


def _compliance_flags(
    post_text: str,
    hashtags: list[str],
    verify: VerifyOut,
    critique: CritiqueOut,
    voice: dict[str, Any],
) -> list[str]:
    """Collect structural / provenance compliance concerns for the report.

    These are non-tone rule breaches the approval email should surface: length
    outside the voice profile's bounds, an out-of-range hashtag count, any
    unsupported claim the verifier flagged, and the editor's residual voice
    concerns. Each is a short, human-readable string.
    """
    flags: list[str] = []

    # Length bounds come from the voice profile (config over code), not constants.
    length = voice.get("structure", {}).get("length_chars", {})
    minimum = length.get("min")
    maximum = length.get("max")
    char_count = len(post_text)
    if minimum is not None and char_count < minimum:
        flags.append(f"length_below_min:{char_count}<{minimum}")
    if maximum is not None and char_count > maximum:
        flags.append(f"length_above_max:{char_count}>{maximum}")

    # The voice structure asks for 3-5 hashtags; anything else is a soft breach.
    tag_count = len(hashtags)
    if tag_count < 3 or tag_count > 5:
        flags.append(f"hashtag_count_out_of_range:{tag_count}")

    # Any claim the verifier could not ground is a first-class compliance signal.
    if verify.unsupported:
        flags.append(f"unsupported_claims_present:{len(verify.unsupported)}")

    # Carry the editor's residual concerns through, namespaced so their origin
    # is obvious in the merged report.
    flags.extend(f"voice:{concern}" for concern in critique.voice_flags)

    return flags


def build_quality_report(
    post_text: str,
    hashtags: list[str],
    verify: VerifyOut,
    critique: CritiqueOut,
    voice: dict[str, Any],
) -> dict[str, Any]:
    """Build the BRD §14.4 ``quality_report`` from the validated pass outputs.

    Pure: given the final text, the verify/critique outputs, and the voice
    config, it returns exactly the eight §14.4 keys — no I/O, no side effects.
    """
    banned = voice.get("banned_phrases", []) or []
    return {
        "char_count": len(post_text),
        # A missing/blank hook is a structural defect the editor should have caught.
        "has_hook": bool(verify.revised_post.hook.strip()),
        "grounding_pct": verify.grounding_pct,
        # Serialise the flagged claims so the report is a plain JSON blob.
        "unsupported_claims": [claim.model_dump() for claim in verify.unsupported],
        "tone_flags": find_banned_phrases(post_text, banned),
        "compliance_flags": _compliance_flags(
            post_text, hashtags, verify, critique, voice
        ),
        "hashtags": list(hashtags),
        "confidence": verify.confidence,
    }


def passes_grounding_gate(grounding_pct: float, settings: Settings) -> bool:
    """Return True iff a grounding percentage meets the configured floor (§13.5).

    A post may auto-publish ONLY when its grounding meets the configured floor
    (``GROUNDING_MIN_PCT``, default 100). Below the floor the post is NOT blocked
    — it is routed to manual approval — so this is an eligibility test, not a
    hard gate, and it is expressed as a single comparison for auditability.

    NOTE: this compares a percentage against the floor and nothing else. The
    percentage it is given MUST be the server-computed value, never the model's
    self-report — see ``compute_grounding_pct`` / ``is_auto_eligible``.
    """
    return grounding_pct >= settings.grounding_min_pct


# The model's self-reported ``grounding_pct`` and the server's own count-derived
# value are allowed to differ by at most this (a hair, for float rounding on
# values like 100 * 2 / 3). Any wider gap means the verifier's headline number
# disagrees with its own claim lists — an inconsistency we refuse to auto-trust.
_GROUNDING_REPORT_TOLERANCE = 0.5


def compute_grounding_pct(verify: VerifyOut) -> float:
    """Derive grounding % SERVER-SIDE from the verifier's own claim counts.

    WHY this exists (BRD §13.5/NFR-01, fail-closed §22.9): the auto-publish
    guarantee is that every rendered post is 100% grounded. Trusting the model's
    self-reported ``grounding_pct`` would let a hallucinated ``100`` auto-publish
    an ungrounded post. So we recompute the number ourselves: the share of ALL
    claims (grounded + unsupported) that are grounded AND passed the verbatim
    check. Only a grounded claim with ``verbatim_ok=True`` counts as truly
    grounded — a grounded-but-non-verbatim claim is NOT.

    Returns 0.0 for the degenerate "no claims at all" case so an empty verdict
    can never clear the floor (fail-closed).
    """
    grounded_ok = sum(1 for claim in verify.grounded if claim.verbatim_ok)
    total_claims = len(verify.grounded) + len(verify.unsupported)
    if total_claims == 0:
        return 0.0
    return 100.0 * grounded_ok / total_claims


def is_auto_eligible(verify: VerifyOut, settings: Settings) -> bool:
    """Decide auto-publish eligibility from SERVER-SIDE grounding facts (§13.5).

    Replaces "trust ``verify.grounding_pct``" with a set of hard, independently
    checkable conditions (all must hold; any failure → manual approval):

      1. ``unsupported == []`` — an ungrounded claim survived verification, so the
         post is by definition not fully grounded.
      2. every grounded claim is ``verbatim_ok`` — a number/date/entity that did
         not match its source exactly is not really grounded.
      3. the model's self-reported ``grounding_pct`` AGREES (within tolerance)
         with the server-computed value — a mismatch means the verifier's
         headline figure contradicts its own claim lists, which we refuse to
         auto-trust (fail-closed §22.9).
      4. the server-computed grounding meets the configured floor.

    The COMPUTED value — never the model's field — drives the floor comparison.
    """
    # (1) Any unsupported claim present ⇒ not fully grounded.
    if verify.unsupported:
        return False

    # (2) A grounded claim that failed the verbatim check is not grounded.
    if not all(claim.verbatim_ok for claim in verify.grounded):
        return False

    computed = compute_grounding_pct(verify)

    # (3) Reject when the self-reported figure disagrees with the computed one.
    if abs(computed - verify.grounding_pct) > _GROUNDING_REPORT_TOLERANCE:
        return False

    # (4) Gate on the server-computed value, not the model's self-report.
    return passes_grounding_gate(computed, settings)

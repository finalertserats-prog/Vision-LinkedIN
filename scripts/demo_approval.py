"""Human-readable Phase-2 demo: compose a sample approval email, mock-send it.

WHY this script exists: it produces a *tangible* sample of the daily approval
email VISION sends the owner (BRD §14.1, Appendix B) — subject, proposed post,
§14.4 quality report, sources, the four signed action buttons, and the expiry
footer — WITHOUT touching a real email provider, LinkedIn, or a database. The
four action links carry REAL signed, single-use, expiring tokens minted by the
production ``approval.tokens`` module, so the sample is faithful to what actually
ships; only the *delivery* is mocked (a recording sender that writes to disk).

Run:
    .venv/Scripts/python scripts/demo_approval.py

Output:
    * the composed HTML email written to ``prep/sample_email.html`` (open it in a
      browser to see the navy/gold themed layout), and
    * a short human-readable summary printed to stdout.

Security (§22): the HMAC secret comes from ``Settings`` (config over code) and is
NEVER printed or written into the artefact. The signed links ARE written into the
sample email — they are demo tokens for a fictional draft id, single-use and
short-TTL, and point only at a GET confirmation route (no state change on GET).

Note on ``print``: operational logs go through the ``logging`` module (§22). The
final summary block is written with ``print`` deliberately — it is this demo's
user-facing deliverable, not debug output.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from vision.approval.tokens import issue_token
from vision.config import Settings
from vision.logging_setup import get_logger
from vision.mailer.composer import SourceRef, compose_approval_email

logger: logging.Logger = get_logger("vision.scripts.demo_approval")

# Where the composed sample email is written (repo ``prep/`` staging dir).
_OUTPUT_PATH: Path = Path(__file__).resolve().parents[1] / "prep" / "sample_email.html"

# A fixed reference "now" so the subject/footer render identically every run.
_NOW: datetime = datetime(2026, 7, 6, 18, 0, 0)

# The signed-link TTL for the demo (2 hours) — real but short, matching the §14.2
# leaked-link mitigation posture.
_TTL_SECONDS: int = 2 * 60 * 60

# A stable fictional draft id so the demo is fully reproducible.
_DEMO_DRAFT_ID: str = "7f3a1c2e-0000-4000-8000-000000000001"

_POST_TEXT: str = (
    "Two operational signals stood out across healthcare and AI today.\n\n"
    "A 200-bed hospital cut claim denials by 18% after moving revenue-cycle "
    "checks to automation. In parallel, an open clinical-note model cleared 223 "
    "evaluation cases — matching prior systems without the licence cost.\n\n"
    "The common thread is unglamorous: put automation where the friction and the "
    "denials actually are, then measure the delta. Pilot one workflow this month."
)
_HASHTAGS: list[str] = ["#HealthcareAI", "#RevenueCycle", "#DigitalHealth"]
_FOCUS: str = "Revenue-cycle management"


@dataclass(frozen=True)
class _DemoDraft:
    """A minimal, immutable stand-in matching the composer's ``_DraftLike`` shape.

    Frozen (immutability principle) and DB-free: the composer only reads a handful
    of attributes, so a tiny value object keeps the demo hermetic — no session, no
    engine, no ORM row — while exactly matching the fields the real ``Draft`` row
    exposes to :func:`compose_approval_email`.
    """

    id: str
    run_id: str
    lane_focus: str | None
    post_text: str | None
    quality_report: dict[str, object] | None
    confidence: float | None
    token_expires_at: datetime | None
    image_type: str
    image_path: str | None


@dataclass
class _RecordingSender:
    """A ``mailer.sender.EmailSender``-shaped mock: records, never sends.

    Satisfies the ``send(subject, text, html, to)`` surface so the demo exercises
    the exact call the daily job makes, but performs NO SMTP/HTTP — the real,
    credential-bearing providers are never imported.
    """

    def __init__(self) -> None:
        self.last: tuple[str, str, str] | None = None

    def send(self, subject: str, text: str, html: str, to: str | None = None) -> bool:
        self.last = (subject, text, html)
        logger.info("mock email 'sent'", extra={"subject": subject, "chars": len(html)})
        return True


def _quality_report() -> dict[str, object]:
    """A §14.4-shaped quality_report so the sample renders a real report block."""
    return {
        "char_count": len(_POST_TEXT),
        "has_hook": True,
        "grounding_pct": 100.0,
        "unsupported_claims": [],
        "tone_flags": [],
        "compliance_flags": [],
        "hashtags": list(_HASHTAGS),
        "confidence": 0.9,
    }


def _sources() -> tuple[SourceRef, ...]:
    """The two grounded sources the sample email lists for spot-checking."""
    return (
        SourceRef(
            title="Hospital cuts claim denials with revenue-cycle automation",
            url="https://example.test/hc/denials-automation",
        ),
        SourceRef(
            title="Open model matches prior systems on clinical-note summarisation",
            url="https://example.test/ai/clinical-notes-model",
        ),
    )


def _signed_links(settings: Settings) -> dict[str, str]:
    """Mint the four REAL signed action links the approval email carries (§14.2).

    Each link is a genuine ``issue_token`` output (single-use, expiring,
    action-scoped) for the demo draft id, wired onto the GET confirmation route.
    The HMAC secret comes from ``settings`` and never leaves this function.
    """
    base = "https://vision.local"
    paths = {"approve": "approve", "post_now": "post-now", "edit": "edit", "reject": "reject"}
    links: dict[str, str] = {}
    for action, path in paths.items():
        token_str, _hash, _exp = issue_token(
            _DEMO_DRAFT_ID, action, _TTL_SECONDS, settings.secret_hmac_key
        )
        links[action] = f"{base}/{path}?token={token_str}"
    return links


def _demo_settings() -> Settings:
    """Pinned settings so the sample renders identically on any machine.

    Uses the dev-default HMAC key deliberately: this is a throwaway demo artefact,
    the tokens are for a fictional draft, and no real profile is reachable.
    """
    return Settings(CARD_BRAND_PALETTE="navy=#0B1F3A;gold=#C9A24B", TZ="Asia/Kolkata")


def main() -> int:
    """Compose the sample approval email, mock-send it, and write the HTML. Returns 0."""
    settings = _demo_settings()

    # The link expiry stamped on the draft footer: two hours from the pinned now,
    # in UTC (the composer localises it to the configured TZ for display).
    expires_at = _NOW.replace(tzinfo=timezone.utc) + timedelta(seconds=_TTL_SECONDS)

    draft = _DemoDraft(
        id=_DEMO_DRAFT_ID,
        run_id=str(uuid.UUID(_DEMO_DRAFT_ID)),
        lane_focus=_FOCUS,
        post_text=_POST_TEXT,
        quality_report=_quality_report(),
        confidence=0.9,
        token_expires_at=expires_at,
        image_type="none",
        image_path=None,
    )

    subject, text, html = compose_approval_email(
        draft,
        _sources(),
        _signed_links(settings),
        settings=settings,
        now=_NOW,
    )

    # Mock-send (no provider, no network), then persist the HTML artefact.
    sender = _RecordingSender()
    accepted = sender.send(subject, text, html)

    _OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _OUTPUT_PATH.write_text(html, encoding="utf-8")
    logger.info("sample email written", extra={"path": str(_OUTPUT_PATH), "bytes": len(html)})

    # The demo's user-facing deliverable (see module docstring on print).
    print("=" * 72)
    print("PROJECT VISION — SAMPLE APPROVAL EMAIL (mock send, no network)")
    print("=" * 72)
    print(f"Subject     : {subject}")
    print(f"Post chars  : {len(_POST_TEXT):,}")
    print(f"Sources     : {len(_sources())}")
    print("Buttons     : Approve / Post now / Edit / Reject (real signed links)")
    print(f"Mock accepted: {accepted}")
    print("-" * 72)
    print(f"HTML email written to: {_OUTPUT_PATH}  ({len(html):,} bytes)")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""``vision-council`` — the COUNCIL CONTENT entry point (BRD §5 evolution).

WHY this module exists: ``vision-daily`` runs the news pipeline (ingest → curate →
synthesise) and emails the owner an approvable draft. The council is the *second*
content mode: a genuine 3-AI deliberation → a de-named, "Powered by Brahmastra"
LinkedIn post. This file is the thin GLUE that runs that deliberation and drops
its result onto the SAME proven rails the daily job already uses:

    run_council()                          # the real 3-AI debate + compose
      -> build a ``pending_approval`` Draft # content_mode='council', council_meta set
      -> mint signed, single-use approval links (approval.tokens)
      -> COMMIT the draft + token keys      # durable BEFORE any email leaves
      -> compose + (mode-gated) send the approval email (FR-20 modes)

It REUSES existing modules end-to-end and rebuilds nothing (BRD §22 reuse rule):
the token minting, the draft state machine value, the mailer, the run-mode gate
and the commit-before-send discipline are all imported from the daily lane, not
re-implemented — so the council inherits every security property the daily job
already proved (signed/expiring/single-use links, no-double-send, fail-closed).

Run modes (FR-20, ``settings.vision_env``) — identical semantics to daily:
  * ``dry_run`` — compose + STORE the draft, but send NO email (safe default).
  * ``staging`` — send the approval email to the owner (self) to exercise the loop.
  * ``live``    — send the approval email for real.
(Publishing is a *separate* process — ``vision-publisher`` — so this job's only
external side effect is the approval email.)

Security (prep/security_threatmodel.md): fail-closed everywhere; no secret is ever
logged; the approval links carry signed, single-use, expiring tokens minted by
``approval.tokens`` — this module only PLACES them, it never invents its own auth.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

# Reuse the daily lane's PROVEN helpers verbatim rather than re-deriving the same
# security-sensitive logic (BRD §22 reuse): the cutoff-TTL resolution and the
# mode-gated send are exactly what the council needs, so importing them keeps ONE
# source of truth for both lanes.
from vision.approval.state_machine import DraftState
from vision.approval.tokens import issue_token
from vision.cli.daily import (
    _DEFAULT_APPROVAL_BASE_URL,
    _as_utc,
    _cutoff_ttl_seconds,
    _send_approval_email,
)
from vision.config import Settings, VisionEnv, get_settings
from vision.council.engine import run_council
from vision.db.models import Draft
from vision.db.session import get_session
from vision.logging_setup import configure_logging, get_logger
from vision.mailer.composer import compose_approval_email
from vision.mailer.sender import EmailSender

logger = get_logger("vision.cli.council")

# The FIVE approval actions a COUNCIL email offers, mapped to their PUBLIC URL
# paths. This is the daily lane's four-action set PLUS ``overrule`` — the
# council-only action that lets the owner supply a one-line counter-take that
# overrides the synthesised post (see mailer.composer._COUNCIL_ACTION_ORDER and
# approval.tokens.VALID_ACTIONS, which already admit ``overrule``). Following the
# established convention (token ``post_now`` -> path ``post-now``), the token
# action word is the key and the hyphenated URL segment is the value. Kept as one
# table so "which action lives at which path" is auditable in a single place; a
# council draft needs its OWN link set (daily's builder mints only four), so we do
# NOT reuse ``daily._build_signed_links`` here.
_COUNCIL_ACTION_PATHS: dict[str, str] = {
    "approve": "approve",
    "post_now": "post-now",
    "edit": "edit",
    "overrule": "overrule",
    "reject": "reject",
}

# The content-mode tag this lane stamps on its drafts (mirrors the engine's own
# constant / the new ``drafts.content_mode`` column). Named, never a raw literal,
# so the CLI and the column default can never silently drift.
_CONTENT_MODE_COUNCIL = "council"

# The state a freshly-composed council draft lands in: it awaits the owner's
# decision via the approval email (§10.4). Sourced from the state-machine enum,
# never a raw string, so the two can never drift.
_STATE_PENDING_APPROVAL = DraftState.PENDING_APPROVAL.value  # "pending_approval"

# Fallback confidence for a council draft. The deliberation is qualitative (a
# thought-leadership debate), so unlike the news lane there is no grounding-%
# score to fold in; we record a neutral, human-review-expected confidence so the
# approval email's confidence field renders something honest. Config-over-code:
# a deployment can raise/lower this via env without touching the pipeline.
_DEFAULT_COUNCIL_CONFIDENCE = 0.6


@dataclass(frozen=True)
class CouncilRunResult:
    """The immutable outcome of one council run (for ``main`` + tests).

    Frozen so a completed run's verdict is a stable artefact a caller/test can
    assert on without re-reading the DB. ``draft_id`` is always populated (a
    council run that produced no draft raised before returning), and
    ``email_sent`` reflects the FR-20 mode gate.
    """

    draft_id: str
    content_mode: str
    email_sent: bool
    topic: str
    format: str


def _build_council_signed_links(
    draft_id: str, settings: Settings, ttl_seconds: int
) -> tuple[dict[str, str], str, datetime]:
    """Mint the FIVE signed council approval links + the approve token's key/expiry.

    Mirrors ``daily._build_signed_links`` exactly — one signed, single-use,
    expiring, action-scoped token per action so an Approve link can never be
    replayed as a Post-now or Overrule (§14.2) — but over the council's FIVE-action
    set (which adds ``overrule``). Only the approve token's *hash* + expiry are
    handed back for persistence on the draft; the raw tokens live solely in the
    email links, never in the DB. Returns
    ``(links, approve_token_hash, approve_expires_at)``.
    """
    base = os.environ.get("VISION_APPROVAL_BASE_URL", _DEFAULT_APPROVAL_BASE_URL).rstrip("/")
    secret = settings.secret_hmac_key
    links: dict[str, str] = {}
    approve_hash = ""
    approve_expires_at = _as_utc(datetime.now(timezone.utc))
    for action, path in _COUNCIL_ACTION_PATHS.items():
        token_str, token_hash, expires_at = issue_token(draft_id, action, ttl_seconds, secret)
        links[action] = f"{base}/{path}?token={token_str}"
        if action == "approve":
            # Only the approve token's single-use key + expiry are persisted on the
            # draft (never the raw token) so the approval loop can verify it later.
            approve_hash = token_hash
            approve_expires_at = expires_at
    return links, approve_hash, approve_expires_at


def _confidence_for(settings: Settings) -> float:
    """Resolve the council draft's confidence, honouring a config override.

    The council has no grounding score to compute a confidence from, so we use a
    neutral default the owner reviews against. Kept a tiny helper so the value has
    a single, testable origin (config-over-code, §22.6).
    """
    override = getattr(settings, "council_confidence", None)
    if isinstance(override, (int, float)):
        return float(override)
    return _DEFAULT_COUNCIL_CONFIDENCE


def _build_council_draft(payload: dict[str, Any], settings: Settings) -> Draft:
    """Map a ``run_council`` payload onto a ``pending_approval`` council Draft.

    Pure assembly (no DB, no network): it copies the de-named public post text +
    hashtags into the published fields and stashes the full provenance —
    ``{topic, format, situation, council_block, transcript}`` — into the new
    ``council_meta`` JSON column, NEVER into ``post_text`` (only the de-named post
    ships). Stamps ``content_mode='council'`` and the pending-approval state so the
    draft enters the SAME approval loop the news lane uses. Kept immutable-friendly:
    fresh ``list()`` copies so the caller's payload is never mutated.
    """
    # Provenance blob for the new column. Built as a plain dict of JSON-safe values
    # (the engine already returns strings/lists/nested dicts) so it round-trips on
    # SQLite and Postgres alike. This is stored for audit and NEVER published.
    council_meta: dict[str, Any] = {
        "topic": payload.get("topic"),
        "format": payload.get("format"),
        "situation": payload.get("situation"),
        "council_block": payload.get("council_block"),
        "transcript": payload.get("transcript"),
    }

    return Draft(
        # No producing ``run`` row: the council is its own cron, not part of a daily
        # run, so ``run_id`` stays NULL (nullable FK) — the provenance lives in
        # ``council_meta`` + ``model_trace`` instead.
        run_id=None,
        # Surface the debated topic as the draft's focus so the approval email's
        # subject/heading reads meaningfully (composer falls back gracefully if empty).
        lane_focus=payload.get("topic"),
        post_text=payload.get("post_text"),
        # Fresh list copies keep the caller's payload immutable (project principle).
        hashtags=list(payload.get("hashtags", [])),
        confidence=_confidence_for(settings),
        state=_STATE_PENDING_APPROVAL,
        # Per-stage provenance from the engine (live voices, chosen format/situation).
        model_trace=payload.get("model_trace"),
        # The two new council columns — the whole point of this lane.
        content_mode=_CONTENT_MODE_COUNCIL,
        council_meta=council_meta,
        # Image lane (§13.6): the engine already decided + generated the visual and
        # wrote the PNG, stamping these fields on the payload. Map them onto the
        # Draft's image_* columns so the mailer + publisher attach the image. A
        # text-only draft carries image_type 'none' with a NULL path (the default),
        # so ``.get(..., 'none')`` keeps an image-less payload valid.
        image_type=payload.get("image_type", "none"),
        image_path=payload.get("image_path"),
        image_source=payload.get("image_source"),
        image_prompt=payload.get("image_prompt"),
    )


def run_council_cli(
    now: datetime,
    mode: VisionEnv,
    *,
    session: Session,
    settings: Settings | None = None,
    sender: EmailSender | None = None,
) -> CouncilRunResult:
    """Run the council once, persist the draft, and (mode-gated) email the owner.

    Orchestration, mirroring the daily lane's commit-before-send discipline:

      1. Run the real 3-AI deliberation + compose (``run_council``). A fail-closed
         engine raises if no genuine council output could be produced, so we never
         email a hollow "council" post — that exception propagates to ``main``.
      2. Assemble a ``pending_approval`` council Draft (content_mode='council',
         ``council_meta`` populated) and flush to assign its id.
      3. Mint the four signed, single-use, expiring approval links for that id and
         persist ONLY the approve token's hash + expiry on the draft (never the raw
         token — it lives solely in the email link).
      4. COMMIT — make the draft + token keys DURABLE before any email leaves the
         building, so the owner can never hold live links to a rolled-back draft.
      5. Compose the approval email and send it per the FR-20 mode (``dry_run``
         sends nothing; ``staging``/``live`` mail the owner).

    Args:
      now: reference instant (injected for determinism; cron passes UTC now).
      mode: the FR-20 run mode governing whether the email is actually sent.
      session: an open SQLAlchemy session. This function COMMITS at the
        commit-before-send point so the draft + single-use token keys are durable
        before the email; ``main`` wraps it in ``get_session`` for the final
        close/rollback boundary.
      settings: config source; defaults to the process singleton.
      sender: injectable email sender so the whole run is unit-testable with NO
        SMTP/HTTP; defaults (lazily, inside the mailer) to the configured provider.

    Returns:
      A frozen :class:`CouncilRunResult` with the draft id, content mode, whether
      an email was sent, and the debated topic/format.

    Raises:
      Propagates any exception from ``run_council`` (fail-closed engine) so a run
      that could not produce a genuine council post fails LOUDLY — ``main`` turns
      that into a non-zero exit so cron alerts rather than silently posting nothing.
    """
    settings = settings or get_settings()
    now = _as_utc(now)
    logger.info("vision-council run starting", extra={"env": mode.value})

    # 1. DELIBERATE + COMPOSE. Fail-closed: a hollow council raises inside the
    #    engine and the exception propagates (never email a fake council post).
    payload = run_council(settings=settings)

    # 2. DRAFT. Build the pending-approval council row and flush for its id (the
    #    approval tokens key on the draft id).
    draft = _build_council_draft(payload, settings)
    session.add(draft)
    session.flush()  # assign the draft PK before minting id-bound tokens
    logger.info(
        "council draft assembled",
        extra={"draft_id": str(draft.id), "content_mode": draft.content_mode},
    )

    # 3. TOKENS. Mint the four signed, single-use, expiring links; persist ONLY the
    #    approve token's single-use key + expiry (never the raw token — §14.2).
    ttl_seconds = _cutoff_ttl_seconds(now, settings)
    links, approve_hash, expires_at = _build_council_signed_links(
        str(draft.id), settings, ttl_seconds
    )
    draft.approve_token_hash = approve_hash
    draft.token_expires_at = expires_at

    # 4. COMMIT-BEFORE-SEND (threat model / §22.9): make the draft + its single-use
    #    token keys DURABLE before the approval email goes out. If this commit fails
    #    the email is never sent, so the owner can never hold live links to a
    #    rolled-back draft.
    session.commit()

    # 5. EMAIL (mode-gated). A council draft has no news ``SourceRef`` list — its
    #    provenance is the council_meta block, not external articles — so we pass an
    #    empty sources sequence (the composer renders "No sources listed"). Any
    #    compose/send failure is logged but never crashes the job (fail-soft email).
    email_sent = False
    try:
        subject, text, html = compose_approval_email(
            draft, [], links, settings=settings, now=now
        )
        email_sent = _send_approval_email(
            mode, sender, settings, subject, text, html, send_deduper=None
        )
    except Exception:  # noqa: BLE001 — a compose/send failure must not lose the draft
        # The draft is already committed (step 4); an email hiccup leaves an
        # approvable draft in place and is surfaced via logs, never a crash.
        logger.exception("council approval email compose/send failed (draft stands)")

    logger.info(
        "vision-council run finalised",
        extra={
            "draft_id": str(draft.id),
            "content_mode": draft.content_mode,
            "email_sent": email_sent,
        },
    )
    return CouncilRunResult(
        draft_id=str(draft.id),
        content_mode=draft.content_mode,
        email_sent=email_sent,
        topic=str(payload.get("topic", "")),
        format=str(payload.get("format", "")),
    )


def main() -> int:
    """``vision-council`` console entry point (its own cron cadence, BRD §5).

    Configures logging, opens one transactional session, runs the council for the
    configured mode, and returns a process exit code so cron can alert on failure.
    A fail-closed engine (no genuine council output) raises; the outer guard turns
    ANY unforeseen failure into a non-zero exit so a bad run alerts rather than
    silently posting nothing.
    """
    configure_logging()
    settings = get_settings()
    mode = settings.vision_env
    logger.info("vision-council invoked", extra={"env": mode.value})

    try:
        with get_session() as session:
            result = run_council_cli(
                datetime.now(timezone.utc),
                mode,
                session=session,
                settings=settings,
            )
    except Exception:
        # Last-resort boundary: the transaction rolled back, nothing half-applied.
        logger.exception("vision-council crashed unexpectedly (fail-closed)")
        return 1

    logger.info(
        "vision-council complete",
        extra={
            "draft_id": result.draft_id,
            "content_mode": result.content_mode,
            "email_sent": result.email_sent,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

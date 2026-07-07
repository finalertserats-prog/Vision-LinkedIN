"""``vision-daily`` — the DAILY ORCHESTRATION entry point (BRD §10.2/§10.3).

WHY this module exists: every other module in VISION does exactly one job well
(fetch feeds, curate, synthesise, render a card, compose an email, …). This file
is the *glue* that wires them into the one journey the ``vision-daily`` cron lives
every morning (~06:30 IST):

    open a run record
      -> INGEST   : FeedFetcher over the enabled sources -> normalise -> persist
      -> CURATE   : select_top (lane-balanced, cross-day suppressed)
      -> SYNTHESE : generate -> critique -> verify (+ quality report + image pass)
      -> DRAFT    : build a ``pending_approval`` draft row
      -> VISUAL   : precision-first image decision + deterministic render (best-effort)
      -> DEDUP    : own-post 90-day similarity check (§11.5 / FR-18)
      -> EMAIL    : compose the approval email + send it (respecting FR-20 modes)
      -> close the run record

It REUSES existing modules end-to-end and rebuilds nothing (BRD §22 reuse rule).

Robustness posture (BRD §10.2 "one failing stage degrades gracefully" + threat
model "never crash-loop"):
  * Each stage is wrapped so a single failure DEGRADES the run to ``partial`` with
    an operator alert instead of aborting the whole job or crashing the cron. A
    stage that leaves nothing to act on (no items / no selection / no draft) ends
    the run early as ``partial``/``failed`` — never a stack trace escaping cron.
  * The image lane can never block a post: any render failure falls back to a
    text-only draft (BRD §13.6).
  * ``main`` catches even the unforeseen so a bad day exits non-zero (cron alerts)
    rather than looping.

Run modes (FR-20, ``settings.vision_env``):
  * ``dry_run`` — full pipeline, but sends NO email and posts nothing (safe default).
  * ``staging`` — emails the owner (self) so the approval loop can be exercised.
  * ``live``    — emails the owner for real.
  (Publishing itself is a *separate* process — ``vision-publisher`` — so the daily
  job's only side effect is the approval email.)

Security (prep/security_threatmodel.md): fail-closed everywhere; no secret is ever
logged (the logging redaction filter + our own care); the approval links carry
signed, single-use, expiring tokens minted by ``approval.tokens`` — this module
only *places* them, it never invents its own auth.
"""

from __future__ import annotations

import hashlib
import html as _html
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import gettempdir
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from vision.approval.state_machine import DraftState
from vision.approval.tokens import issue_token
from vision.brahmastra.client import BrahmastraClient
from vision.brahmastra.errors import BrahmastraError
from vision.brahmastra.image_client import BrahmastraImageClient
from vision.config import Settings, VisionEnv, get_settings
from vision.curate.dedup import normalise_url
from vision.curate.own_dedup import check_against_own
from vision.curate.score import ScoringConfig
from vision.curate.select import select_top
from vision.db.models import Draft, Item, Run
from vision.db.session import get_session
from vision.ingest.feeds import FeedFetcher, FetchResult
from vision.ingest.normalise import normalise_many
from vision.ingest.sources import get_enabled_sources
from vision.logging_setup import configure_logging, get_logger
from vision.mailer.composer import SourceRef, compose_approval_email
from vision.mailer.dedup import SendDeduper
from vision.mailer.sender import EmailSender, get_sender
from vision.ops.joblock import acquire_job_lock, date_key, release_job_lock
from vision.synthesise.pipeline import synthesise
from vision.synthesise.prompts import PromptLibrary
from vision.visuals.card_renderer import render_stat_card
from vision.visuals.decide import CardSpec, Datapoint, ImageType, image_decision
from vision.visuals.illustrate import generate_illustration

logger = get_logger("vision.cli.daily")

# Name of the singleton cron job this module IS — used as the atomic run-lock key
# (threat model §4 "prevent overlapping cron runs with an atomic lock") so two
# overlapping ``vision-daily`` processes can never both mint an approvable draft.
_JOB_NAME = "vision-daily"

# Where the per-day idempotency key is stashed on a draft. Lives inside the
# existing ``quality_report`` JSON (no schema change; portable SQLite/Postgres) so
# a same-day re-run can find and REUSE today's pending draft rather than minting a
# second approvable one. Underscore-prefixed so the email's quality renderer — which
# only reads named metric keys — never displays it.
_IDEMPOTENCY_REPORT_KEY = "_idempotency_key"

# --- Config-over-code knobs (BRD §22.6) ------------------------------------
# The state a freshly-orchestrated draft lands in: it awaits the owner's decision
# via the approval email (§10.4). Named from the state-machine enum, never a raw
# literal, so the two can never drift.
_STATE_PENDING_APPROVAL = DraftState.PENDING_APPROVAL.value  # "pending_approval"

# How many source items feed one blended draft. Small by design — a post is a
# focused HC×AI take, not a digest — and overridable via env for tuning.
_DEFAULT_SELECT_K = 3

# The four approval actions the email must offer, mapped to their PUBLIC URL paths
# (note ``post_now`` -> ``/post-now``, matching approval/web.py's route table).
# Kept as one table so "which action lives at which path" is auditable in one place.
_ACTION_PATHS: dict[str, str] = {
    "approve": "approve",
    "post_now": "post-now",
    "edit": "edit",
    "reject": "reject",
}

# Default public base URL for the approval links. The always-on ``vision-web``
# service serves these routes; the real deployment sets VISION_APPROVAL_BASE_URL
# to its externally-reachable HTTPS origin (config over code).
_DEFAULT_APPROVAL_BASE_URL = "http://localhost:8000"

# Env var naming the directory rendered card/illustration PNGs are written to (the
# publisher later reads ``draft.image_path`` from here). Defaults under the system
# temp dir so a bare checkout works; prod points it at a durable volume.
_IMAGE_DIR_ENV = "VISION_IMAGE_DIR"

# When the approval cutoff cannot be resolved (misconfig), fall back to a generous
# but finite token TTL so a draft never ships with an already-dead or endless link.
_TTL_FALLBACK_SECONDS = 6 * 60 * 60  # 6 hours

# The rotating daily focus (§13.2) that anchors the post. A small, editable pool
# picked deterministically by day-of-year; VISION_DAILY_FOCUS overrides it outright.
_FOCUS_ROTATION: tuple[str, ...] = (
    "Healthcare operations × AI",
    "Clinical AI & patient safety",
    "Health-system economics",
    "AI tooling for builders",
    "Regulation & trust in health AI",
)


@dataclass(frozen=True)
class RunResult:
    """The immutable outcome of one daily orchestration (for main + tests).

    Frozen so a completed run's verdict is a stable artefact. ``stats`` mirrors what
    is persisted on ``runs.stats`` (JSON-safe) so callers can assert on it without
    re-reading the DB.
    """

    run_id: str
    status: str  # "ok" | "partial" | "failed"
    draft_id: str | None
    email_sent: bool
    alerts: tuple[str, ...]
    stats: dict[str, Any]


# ---------------------------------------------------------------------------
# Small pure helpers.
# ---------------------------------------------------------------------------


def _as_utc(moment: datetime) -> datetime:
    """Return ``moment`` as aware UTC, assuming UTC for a naive value.

    Cron passes ``datetime.now(timezone.utc)`` (aware); tests may pin an aware
    value. A naive value is treated as UTC so downstream arithmetic never mixes a
    naive local time with an aware one (the classic tz bug).
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _image_dir() -> Path:
    """Resolve the directory for rendered images (config over code, pathlib)."""
    configured = os.environ.get(_IMAGE_DIR_ENV)
    return Path(configured) if configured else Path(gettempdir()) / "vision" / "images"


def _focus_for(now: datetime) -> str:
    """Pick the day's rotating focus (§13.2), honouring a VISION_DAILY_FOCUS override."""
    override = os.environ.get("VISION_DAILY_FOCUS")
    if override and override.strip():
        return override.strip()
    # Deterministic by day-of-year so the same date always yields the same focus
    # (reproducible runs; no hidden randomness).
    index = now.timetuple().tm_yday % len(_FOCUS_ROTATION)
    return _FOCUS_ROTATION[index]


def _cutoff_ttl_seconds(now: datetime, settings: Settings) -> int:
    """Seconds from ``now`` until today's approval cutoff in the owner's timezone.

    The signed approval links must die at the daily cutoff (default 20:00 IST) so a
    leaked link cannot act tomorrow (§14.2). We resolve the cutoff as a wall-clock
    time in ``settings.tz`` on ``now``'s local date and return the remaining
    seconds. Any tz/parse problem, or an already-passed cutoff, falls back to a
    finite default rather than minting an endless or already-dead token (fail-safe).
    """
    now_utc = _as_utc(now)
    try:
        tz = ZoneInfo(settings.tz)
        hours_raw, _, minutes_raw = settings.approve_cutoff_local.partition(":")
        local_now = now_utc.astimezone(tz)
        cutoff_local = local_now.replace(
            hour=int(hours_raw), minute=int(minutes_raw), second=0, microsecond=0
        )
        remaining = (cutoff_local.astimezone(timezone.utc) - now_utc).total_seconds()
    except (KeyError, ValueError) as exc:
        # Unknown tz name (KeyError) or malformed cutoff (ValueError) — never guess
        # a silent surprise cutoff; use the finite fallback and log.
        logger.warning("cutoff resolve failed (%s); using fallback TTL", exc.__class__.__name__)
        return _TTL_FALLBACK_SECONDS
    if remaining <= 0:
        # Cron ran after the cutoff (unusual) — give a finite window rather than a
        # dead link, so a late run still yields an actionable email.
        return _TTL_FALLBACK_SECONDS
    return int(remaining)


def _synth_item(item: Item) -> dict[str, Any]:
    """Map a persisted ``Item`` onto the dict shape the synthesis passes expect.

    ``source_item_id`` is the item's real UUID so the model's claims cite genuine
    provenance the grounding gate can verify (§13.5). ``item.source`` is loaded
    lazily from the open session; a source-less item (source_id NULL) degrades to a
    ``None`` source name rather than raising.
    """
    return {
        "source_item_id": str(item.id),
        "title": item.title,
        "url": item.url,
        "source": getattr(item.source, "name", None),
        "published_at": item.published_at.isoformat() if item.published_at else None,
        "summary": item.summary,
    }


def _visuals_card_spec(card_spec: dict[str, Any]) -> CardSpec:
    """Re-validate a synthesised card_spec dict through the renderer's own model.

    The synthesis and visuals lanes share a structurally-identical card schema;
    passing the synthesised spec back through the visuals ``CardSpec``/``Datapoint``
    keeps the render strictly grounded (every datapoint carries its source id).
    """
    return CardSpec(
        title=str(card_spec.get("title", "")),
        datapoints=[
            Datapoint(
                label=str(point.get("label", "")),
                value=str(point.get("value", "")),
                source_item_id=(
                    str(point["source_item_id"]) if point.get("source_item_id") else None
                ),
            )
            for point in card_spec.get("datapoints", [])
        ],
        source_label=card_spec.get("source_label"),
    )


def _build_signed_links(
    draft_id: str, settings: Settings, ttl_seconds: int
) -> tuple[dict[str, str], str, datetime]:
    """Mint the four signed approval links and return them + the approve token's key.

    Each action gets its own signed, single-use, expiring, action-scoped token
    (approval/tokens.issue_token) so an Approve link can never be replayed as a
    Post-now (§14.2). Only the approve token's *hash* + expiry are handed back for
    persistence on the draft — the raw tokens live solely in the email links, never
    in the DB (the store keeps only the single-use key). Returns
    ``(links, approve_token_hash, approve_expires_at)``.
    """
    base = os.environ.get("VISION_APPROVAL_BASE_URL", _DEFAULT_APPROVAL_BASE_URL).rstrip("/")
    secret = settings.secret_hmac_key
    links: dict[str, str] = {}
    approve_hash = ""
    approve_expires_at = _as_utc(datetime.now(timezone.utc))
    for action, path in _ACTION_PATHS.items():
        token_str, token_hash, expires_at = issue_token(draft_id, action, ttl_seconds, secret)
        links[action] = f"{base}/{path}?token={token_str}"
        if action == "approve":
            approve_hash = token_hash
            approve_expires_at = expires_at
    return links, approve_hash, approve_expires_at


# ---------------------------------------------------------------------------
# Idempotency (no-double-post across retried / overlapping runs).
# ---------------------------------------------------------------------------


def _idempotency_key(now: datetime, items: list[Item], focus: str) -> str:
    """Derive the day's stable idempotency key from ``date + item-set + focus``.

    WHY a *content* key, not a run/item-UUID key (threat model §4 / "idempotency
    keys prevent duplicate posts"): every run re-ingests the feeds and mints BRAND
    NEW ``Item`` rows, so keying on item UUIDs would never match across runs. We
    key on each item's NORMALISED URL — the stable natural identity the dedup layer
    already trusts — so two runs on the same day over the same stories hash to the
    SAME key and the second run reuses the first run's draft instead of minting a
    second approvable one. The UTC ``date`` scopes it to one day and the ``focus``
    means a genuinely different-angle day still gets its own draft.
    """
    urls = sorted(normalise_url(getattr(item, "url", "") or "") for item in items)
    # \x1f (unit separator) can never appear in a URL, date, or focus string, so
    # the fields can never run together into an ambiguous pre-image.
    pre_image = "\x1f".join((date_key(now), focus, *urls))
    return hashlib.sha256(pre_image.encode("utf-8")).hexdigest()


def _find_reusable_draft(session: Session, idem_key: str) -> Draft | None:
    """Return today's already-minted pending draft for ``idem_key``, or ``None``.

    A re-run (or a run that started after a sibling committed its draft) must REUSE
    the existing ``pending_approval`` draft rather than create a second approvable
    one. We scan only pending drafts — a tiny set for a once-daily job — and match
    the key stashed in ``quality_report``. Matching by the stored key (not by
    re-deriving from the draft's per-run item UUIDs) is what makes the reuse robust.
    """
    pending = (
        session.query(Draft).filter(Draft.state == _STATE_PENDING_APPROVAL).all()
    )
    for draft in pending:
        report = draft.quality_report or {}
        if report.get(_IDEMPOTENCY_REPORT_KEY) == idem_key:
            return draft
    return None


# ---------------------------------------------------------------------------
# Persistence helpers.
# ---------------------------------------------------------------------------


def _persist_items(
    session: Session,
    run: Run,
    sources: list[Any],
    fetch_result: FetchResult,
) -> list[Item]:
    """Normalise + persist the fetched signals into ``items`` and stamp feed health.

    Each normalised signal becomes an ``Item`` tied to its originating source (by
    name) and this run. Successful sources have ``last_ok_at`` stamped from the
    fetch health (§17 feed-health), so a source silent for too long is visible to
    ops. The caller owns the transaction; this flushes so item ids are assigned for
    downstream provenance.
    """
    by_name = {src.name: src for src in sources}
    normalised = normalise_many(fetch_result.items)

    items: list[Item] = []
    for signal in normalised:
        source = by_name.get(signal.source)
        item = Item(
            source_id=source.id if source is not None else None,
            run_id=run.id,
            lane=signal.lane,
            title=signal.title,
            url=signal.url,
            published_at=signal.published_at,
            summary=signal.summary,
            content_hash=signal.content_hash,
        )
        session.add(item)
        items.append(item)

    # Feed-health -> sources.last_ok_at (only for sources that actually succeeded).
    for health in fetch_result.health.values():
        source = by_name.get(health.name)
        if source is not None and health.ok:
            source.last_ok_at = health.checked_at

    session.flush()  # assign item PKs so their UUIDs are usable as provenance
    return items


# ---------------------------------------------------------------------------
# Image lane (best-effort; never blocks a post).
# ---------------------------------------------------------------------------


def _render_image(
    draft: Draft,
    draft_dict: dict[str, Any],
    settings: Settings,
    image_client: BrahmastraImageClient | None,
    run: Run,
) -> None:
    """Resolve + render the draft's image, degrading to text-only on any problem.

    Applies the precision-first gate (visuals.decide.image_decision): numbers are
    only ever rendered as a DETERMINISTIC card, never handed to a diffusion model
    (§13.6). Mutates the draft's image_* columns in place. Raises nothing the caller
    must handle for a soft failure — a concept illustration that fails to generate
    just leaves the draft text-only (BRD §13.6: an image never blocks publishing).
    """
    if not settings.image_enabled:
        return

    decision = draft_dict.get("image_decision") or {}
    post_text = draft_dict.get("post_text") or ""
    card = decision.get("card_spec") or {}
    # Feed the gate the card's own datapoints as claim text so its number-detection
    # sees exactly what a card would render.
    claim_texts = [
        f"{point.get('label', '')} {point.get('value', '')}"
        for point in card.get("datapoints", [])
    ]

    final_type = image_decision(post_text, claim_texts, decision)
    if final_type is ImageType.NONE:
        return

    image_dir = _image_dir()
    image_dir.mkdir(parents=True, exist_ok=True)
    out_path = image_dir / f"{run.id}_{draft.id}.png"

    if final_type is ImageType.INFORMATIVE_CARD:
        # Deterministic render — the ONLY path allowed to carry numbers (§13.6).
        spec = _visuals_card_spec(card)
        png = render_stat_card(spec, settings)
        out_path.write_bytes(png)
        draft.image_type = ImageType.INFORMATIVE_CARD.value
        draft.image_path = str(out_path)
        draft.image_source = "deterministic"
        return

    # CONCEPT_ILLUSTRATION: text-free diffusion image, degrade-gracefully to None.
    prompt = draft_dict.get("image_prompt") or decision.get("illustration_prompt") or ""
    png = generate_illustration(prompt, client=image_client, settings=settings)
    if png is None:
        # Generation skipped/failed — leave the draft text-only (never blocks).
        return
    out_path.write_bytes(png)
    draft.image_type = ImageType.CONCEPT_ILLUSTRATION.value
    draft.image_path = str(out_path)
    draft.image_source = settings.image_model
    draft.image_prompt = prompt


# ---------------------------------------------------------------------------
# Email + alerting.
# ---------------------------------------------------------------------------


def _resolve_sender(sender: EmailSender | None, settings: Settings) -> EmailSender:
    """Return the injected sender, or build one from config lazily (tests inject)."""
    return sender if sender is not None else get_sender(settings)


def _send_approval_email(
    mode: VisionEnv,
    sender: EmailSender | None,
    settings: Settings,
    subject: str,
    text: str,
    html: str,
    send_deduper: SendDeduper | None,
) -> bool:
    """Send the approval email per the FR-20 mode; return whether it was sent.

    ``dry_run`` sends nothing (the safe default). ``staging`` and ``live`` both mail
    the owner. A ``SendDeduper`` (when supplied) makes a same-day re-run idempotent
    from the owner's inbox (BRD §14.5) — the mark is written ONLY after the provider
    accepts, so a failed send never suppresses its retry.
    """
    if mode is VisionEnv.DRY_RUN:
        logger.info("dry_run: approval email suppressed (no send)")
        return False

    if send_deduper is not None and send_deduper.is_suppressed(subject):
        logger.info("approval email suppressed by send-dedup (same-day re-run)")
        return False

    # Capture the provider's delivery verdict FIRST. Everything after this point is
    # bookkeeping — it must never be able to flip a real delivery back to "unsent".
    delivered = _resolve_sender(sender, settings).send(subject, text, html)
    if delivered and send_deduper is not None:
        # Record ONLY after acceptance so a failed send can still be retried. Guard
        # the marker write in its OWN try/except: if ``mark_sent`` raises (e.g. an
        # unwritable state dir), the email has ALREADY gone out — letting that
        # exception bubble would (a) drive the caller to record email_sent=False and
        # (b) leave the dedup UNMARKED, so a same-day retry would DUPLICATE the email.
        # We log it and keep ``delivered`` true. Never log the subject at error level
        # verbatim beyond what the redaction filter already scrubs; it carries no secret.
        try:
            send_deduper.mark_sent(subject)
        except (OSError, SQLAlchemyError, ValueError, RuntimeError):
            logger.exception(
                "send-dedup mark_sent failed AFTER a delivered email; "
                "delivery stands, but same-day dedup may not survive a restart"
            )
    return delivered


def _send_alert_summary(
    mode: VisionEnv,
    sender: EmailSender | None,
    settings: Settings,
    alerts: list[str],
) -> None:
    """Email the owner a single summary of any degraded stages. Never raises.

    Fired once at the end of a partial/failed run so a degraded pipeline is visible
    without spamming one email per stage. Suppressed in ``dry_run`` (FR-20: no
    email). Carries ONLY the short, non-secret stage messages — the redaction filter
    plus our own care keep credentials out of it. A mail-provider failure here must
    never crash the job, so everything is guarded.
    """
    if mode is VisionEnv.DRY_RUN or not alerts:
        return
    subject = "VISION daily — run degraded (partial)"
    body = "The daily run completed with degraded stages:\n\n" + "\n".join(
        f"  - {line}" for line in alerts
    )
    html = "<p>The daily run completed with degraded stages:</p><ul>" + "".join(
        f"<li>{_html.escape(line)}</li>" for line in alerts
    ) + "</ul>"
    try:
        _resolve_sender(sender, settings).send(subject, body, html)
    except Exception:  # noqa: BLE001 — a monitoring alert must never crash the run
        logger.exception("failed to send degraded-run alert (run outcome unaffected)")


# ---------------------------------------------------------------------------
# Finalisation.
# ---------------------------------------------------------------------------


def _finalize(
    session: Session,
    run: Run,
    status: str,
    *,
    mode: VisionEnv,
    sender: EmailSender | None,
    settings: Settings,
    draft_id: str | None,
    email_sent: bool,
    alerts: list[str],
    stats: dict[str, Any],
) -> RunResult:
    """Close the run record durably, fire any degraded-run alert, and return.

    The run's status/stats/notes are persisted and COMMITTED here — deliberately
    BEFORE the degraded-run alert email is sent (commit-before-send / §22.9): the
    alert is an external side effect, so committing the run record first means a
    later mail-provider hiccup can never roll back the durable run outcome, and this
    doubles as the durable ``email_sent`` marker for the approval-email path (which
    stamps ``email_sent`` right before calling us). The summary alert is sent here so
    EVERY exit path — early stage failure or a full run — surfaces a degraded
    outcome exactly once.
    """
    stats["status"] = status
    stats["draft_id"] = draft_id
    stats["email_sent"] = email_sent
    stats["alerts"] = list(alerts)
    stats["finished_at"] = datetime.now(timezone.utc).isoformat()

    run.status = status
    # New dict so SQLAlchemy reliably detects the JSON column change before commit.
    run.stats = dict(stats)
    run.notes = f"daily {mode.value}: {status}"

    # Durable commit BEFORE any external email leaves the building.
    session.commit()

    # One alert for the whole run (no-op in dry_run / when clean).
    _send_alert_summary(mode, sender, settings, alerts)

    logger.info(
        "daily run finalised",
        extra={"run_id": str(run.id), "status": status, "alerts": len(alerts)},
    )
    return RunResult(
        run_id=str(run.id),
        status=status,
        draft_id=draft_id,
        email_sent=email_sent,
        alerts=tuple(alerts),
        stats=stats,
    )


# ---------------------------------------------------------------------------
# The orchestration.
# ---------------------------------------------------------------------------


def run_daily(
    now: datetime,
    mode: VisionEnv,
    *,
    session: Session,
    settings: Settings | None = None,
    fetcher: FeedFetcher | None = None,
    brahmastra: BrahmastraClient | None = None,
    sender: EmailSender | None = None,
    image_client: BrahmastraImageClient | None = None,
    prompts: PromptLibrary | None = None,
    send_deduper: SendDeduper | None = None,
    select_k: int = _DEFAULT_SELECT_K,
    lock_dir: Path | None = None,
) -> RunResult:
    """Run the full daily pipeline once, under an atomic per-day run lock.

    WHY the lock wraps everything (threat model §4 / Hardening Checklist
    "concurrency lock"): the pipeline's one external effect is emailing the owner an
    *approvable* draft with live single-use LinkedIn links. Two overlapping runs
    (a slow run, a manual re-run racing cron, a restart storm) would each mint a
    separate approvable draft and the owner could approve BOTH → duplicate post. We
    acquire an atomic ``O_CREAT|O_EXCL`` lockfile keyed on (job, UTC-date); if a
    sibling already holds it we SKIP rather than mint an overlapping draft. The lock
    is always released (even on a raise) so a crash can never wedge the next day.

    Args:
        now: the reference instant (injected for determinism; cron passes UTC now).
        mode: the run mode (FR-20) governing side effects.
        session: an open SQLAlchemy session. ``run_daily`` COMMITS at the
            commit-before-send point (so the draft + single-use tokens are durable
            before any email leaves the building) and again to record the durable
            ``email_sent`` marker; ``main`` still wraps it in ``get_session`` for the
            final close/rollback boundary.
        settings: config source; defaults to the process singleton.
        fetcher / brahmastra / sender / image_client / prompts: injectable
            collaborators so the whole run is unit-testable with NO network, NO
            model, and NO real email (BRD §18). Each defaults to the real object.
        send_deduper: optional same-day email dedup (BRD §14.5); omit to always send.
        select_k: how many source items to select for the draft.
        lock_dir: override for the run-lock directory (config-over-code / tests);
            defaults to ``VISION_LOCK_DIR`` or the system temp dir.

    Never raises for an operational failure: a failing stage degrades the run to
    ``partial`` (or ``failed`` when nothing could be produced) with an operator
    alert, so the cron never crash-loops (BRD §10.2, threat model).
    """
    settings = settings or get_settings()
    now = _as_utc(now)
    logger.info("vision-daily run starting", extra={"env": mode.value})

    # Atomic overlap guard: acquire today's run lock BEFORE any work. A sibling
    # holding it means another vision-daily is already running today — skip cleanly.
    lock = acquire_job_lock(_JOB_NAME, now, lock_dir=lock_dir)
    if lock is None:
        return _skipped_run(session, mode)
    try:
        return _run_pipeline(
            now, mode,
            session=session, settings=settings,
            fetcher=fetcher, brahmastra=brahmastra, sender=sender,
            image_client=image_client, prompts=prompts,
            send_deduper=send_deduper, select_k=select_k,
        )
    finally:
        # ALWAYS release — a raised body must not leave the lockfile behind and
        # block tomorrow's run (the stale-breaker is only a backstop, not the plan).
        release_job_lock(lock)


def _skipped_run(session: Session, mode: VisionEnv) -> RunResult:
    """Record and return a benign ``skipped`` outcome (a sibling holds the lock).

    A skipped run is NOT a failure — another process is handling today — so we
    persist a tiny audit row and return a clean result. ``main`` treats ``skipped``
    as a zero exit so cron does not alert on a benign overlap.
    """
    skip_run = Run(
        status="skipped",
        stats={"skipped": "job lock held by a concurrent run"},
        notes=f"daily {mode.value}: skipped (lock held)",
    )
    session.add(skip_run)
    session.flush()
    logger.warning("vision-daily skipped: another run holds today's lock")
    return RunResult(
        run_id=str(skip_run.id),
        status="skipped",
        draft_id=None,
        email_sent=False,
        alerts=("lock: another vision-daily run is already in progress",),
        stats={"mode": mode.value, "skipped": True},
    )


def _run_pipeline(
    now: datetime,
    mode: VisionEnv,
    *,
    session: Session,
    settings: Settings,
    fetcher: FeedFetcher | None,
    brahmastra: BrahmastraClient | None,
    sender: EmailSender | None,
    image_client: BrahmastraImageClient | None,
    prompts: PromptLibrary | None,
    send_deduper: SendDeduper | None,
    select_k: int,
) -> RunResult:
    """The daily pipeline body, run while the per-day lock is held (see run_daily)."""
    # Open the run record up front so even a stage-1 failure has a durable row to
    # attach status/stats to (observability, §11.3).
    run = Run(status="ok", stats={}, notes=f"daily {mode.value}: started")
    session.add(run)
    session.flush()  # assign the run PK before items/drafts reference it

    alerts: list[str] = []
    stats: dict[str, Any] = {"mode": mode.value, "started_at": now.isoformat()}

    def _degrade(stage: str, message: str, exc: BaseException | None = None) -> None:
        """Record a degraded stage (partial run + alert). Logs, never raises."""
        line = f"{stage}: {message}"
        if exc is not None:
            logger.exception("stage degraded: %s", line)
        else:
            logger.warning("stage degraded: %s", line)
        alerts.append(line)

    # ----------------------------------------------------------------- INGEST
    items: list[Item] = []
    try:
        sources = get_enabled_sources(session)
        if not sources:
            # No enabled sources is a configuration problem, not a crash — degrade.
            _degrade("ingest", "no enabled sources configured")
        else:
            active_fetcher = fetcher if fetcher is not None else FeedFetcher()
            fetch_result = active_fetcher.fetch_all(sources)
            items = _persist_items(session, run, sources, fetch_result)
            unhealthy = [h.name for h in fetch_result.health.values() if not h.ok]
            stats["ingest"] = {
                "sources": len(sources),
                "fetched": len(fetch_result.items),
                "persisted": len(items),
                "unhealthy": unhealthy,
            }
            if unhealthy:
                # A dead feed must not kill the batch (SC7/NFR-07) — record + continue
                # with whatever the healthy sources returned.
                _degrade("ingest", f"{len(unhealthy)} source(s) failed: {unhealthy}")
    except Exception as exc:  # noqa: BLE001 — a broken ingest degrades, never crashes
        _degrade("ingest", "ingest stage failed", exc)

    if not items:
        # Nothing to write about — end early. ``failed`` when ingest produced zero
        # items at all; the alert(s) already explain why.
        return _finalize(
            session, run, "failed",
            mode=mode, sender=sender, settings=settings,
            draft_id=None, email_sent=False, alerts=alerts, stats=stats,
        )

    # ------------------------------------------------------------ IDEMPOTENCY
    # Before doing any generative work, check whether TODAY already produced a
    # pending draft for this exact (date + ingested-item-set + focus). WHY here —
    # after ingest, before curate: a re-run re-ingests the same stories (stable by
    # URL) but curate's own cross-day suppression would hide them, so we key on the
    # ingested set, not the curated one. Reusing the existing draft means a retried
    # or just-missed-the-lock run can never mint a SECOND approvable draft + a
    # SECOND set of single-use tokens for the same day (no double-post).
    focus = _focus_for(now)
    idem_key = _idempotency_key(now, items, focus)
    existing = _find_reusable_draft(session, idem_key)
    if existing is not None:
        logger.info(
            "vision-daily reusing today's pending draft (idempotent re-run)",
            extra={"run_id": str(run.id), "draft_id": str(existing.id)},
        )
        stats["idempotent_reuse"] = True
        # No synthesis, no new draft, NO new tokens, no second email — reuse only.
        return _finalize(
            session, run, "ok",
            mode=mode, sender=sender, settings=settings,
            draft_id=str(existing.id), email_sent=False, alerts=alerts, stats=stats,
        )

    # ----------------------------------------------------------------- CURATE
    selected: list[Item] = []
    try:
        selection = select_top(
            items,
            k=select_k,
            config=ScoringConfig.load(settings),
            session=session,
            now=now,
        )
        selected = list(selection.selected)
        stats["curate"] = selection.rationale
    except Exception as exc:  # noqa: BLE001 — curate failure degrades the run
        _degrade("curate", "curate stage failed", exc)

    if not selected:
        _degrade("curate", "no items selected for a draft")
        return _finalize(
            session, run, "partial",
            mode=mode, sender=sender, settings=settings,
            draft_id=None, email_sent=False, alerts=alerts, stats=stats,
        )

    # ------------------------------------------------------------- SYNTHESISE
    # ``focus`` and ``idem_key`` were resolved above (idempotency guard).
    synth_items = [_synth_item(item) for item in selected]
    try:
        active_brahmastra = brahmastra if brahmastra is not None else BrahmastraClient(settings)
        draft_dict = synthesise(
            focus,
            synth_items,
            client=active_brahmastra,
            settings=settings,
            prompts=prompts,
        )
    except BrahmastraError as exc:
        # A total synthesis outage (all lanes exhausted / schema drift) — nothing to
        # approve. Degrade to partial (curate succeeded) and end.
        _degrade("synthesise", "synthesis failed", exc)
        return _finalize(
            session, run, "partial",
            mode=mode, sender=sender, settings=settings,
            draft_id=None, email_sent=False, alerts=alerts, stats=stats,
        )
    except Exception as exc:  # noqa: BLE001 — any unforeseen synth error degrades
        _degrade("synthesise", "synthesis crashed", exc)
        return _finalize(
            session, run, "partial",
            mode=mode, sender=sender, settings=settings,
            draft_id=None, email_sent=False, alerts=alerts, stats=stats,
        )

    # ------------------------------------------------------------------ DRAFT
    # Build the pending-approval draft row now so it has an id the image filename
    # and the approval tokens can key on. Stamp the day's idempotency key into the
    # quality report so a later same-day re-run REUSES this draft (no double-post).
    quality_report = dict(draft_dict.get("quality_report") or {})
    quality_report[_IDEMPOTENCY_REPORT_KEY] = idem_key
    draft = Draft(
        run_id=run.id,
        lane_focus=draft_dict.get("lane_focus", focus),
        post_text=draft_dict.get("post_text"),
        hashtags=list(draft_dict.get("hashtags", [])),
        source_item_ids=list(draft_dict.get("source_item_ids", [])),
        quality_report=quality_report,
        confidence=draft_dict.get("confidence"),
        state=_STATE_PENDING_APPROVAL,
        model_trace=draft_dict.get("model_trace"),
        image_type=ImageType.NONE.value,
    )
    session.add(draft)
    session.flush()  # assign the draft PK
    stats["auto_eligible"] = bool(draft_dict.get("auto_eligible"))

    # ------------------------------------------------------------------ IMAGE
    try:
        _render_image(draft, draft_dict, settings, image_client, run)
    except Exception as exc:  # noqa: BLE001 — an image never blocks a post (§13.6)
        _degrade("image", "image render failed; posting text-only", exc)
        draft.image_type = ImageType.NONE.value
        draft.image_path = None

    # ------------------------------------------------------------- OWN-DEDUP
    # FR-18: guard against semantically duplicating one of the owner's own posts in
    # the last 90 days. Fold the verdict into the quality report so the approval
    # email shows it, and flag a near-duplicate for the owner's attention. This is a
    # SAFETY gate: if it cannot produce a trustworthy verdict we must fail CLOSED
    # (below) rather than email an approvable draft on an unverified check (§22.9).
    dedup_trustworthy = True
    try:
        dedup = check_against_own(session, draft.post_text or "")
        report = dict(draft.quality_report or {})
        report["dedup_vs_own_90d"] = {
            "pass": bool(dedup.get("pass")),
            "max_similarity": round(float(dedup.get("max_similarity", 0.0)), 4),
            "nearest_urn": dedup.get("nearest_urn"),
        }
        # Immutable update so SQLAlchemy detects the JSON column change.
        draft.quality_report = report
        if not dedup.get("pass"):
            # A near-duplicate is a HUMAN-review signal (surfaced in the email's
            # quality report as REVIEW) — the owner still decides, so we alert but
            # do NOT withhold. Only an unavailable verdict (below) fails closed.
            _degrade(
                "dedup",
                f"draft resembles a recent own post (max sim "
                f"{round(float(dedup.get('max_similarity', 0.0)), 3)})",
            )
    except Exception as exc:  # noqa: BLE001 — dedup failure degrades, never crashes
        # We have NO trustworthy FR-18 verdict → fail closed: do not emit an
        # approvable email for a draft whose no-double-post safety could not be
        # checked. Recorded here; enforced at the EMAIL gate below.
        dedup_trustworthy = False
        _degrade("dedup", "own-post safety verdict unavailable; withholding approval email", exc)

    # ------------------------------------------------------------------ EMAIL
    # Fail-closed gate (§22.9): with no trustworthy dedup verdict, leave the draft
    # NON-ACTIONABLE — mint no token, send no approval email. The raw approve token
    # only ever lives in the email, so an un-emailed draft simply cannot be approved.
    if not dedup_trustworthy:
        return _finalize(
            session, run, "partial",
            mode=mode, sender=sender, settings=settings,
            draft_id=str(draft.id), email_sent=False, alerts=alerts, stats=stats,
        )

    ttl_seconds = _cutoff_ttl_seconds(now, settings)
    links, approve_hash, expires_at = _build_signed_links(str(draft.id), settings, ttl_seconds)
    # Persist the approve token's single-use key + expiry (never the raw token).
    draft.approve_token_hash = approve_hash
    draft.token_expires_at = expires_at

    # COMMIT-BEFORE-SEND (threat model / §22.9): make the draft + its single-use
    # token keys DURABLE *before* the approval email goes out. If this commit fails
    # the email is never sent, so the owner can never hold live links to a
    # rolled-back draft and a clean retry (idempotency-guarded) starts fresh. If it
    # succeeds, the emitted links are backed by a persisted, approvable draft.
    session.commit()

    source_refs = [SourceRef(title=item.title, url=item.url) for item in selected]

    email_sent = False
    try:
        subject, text, html = compose_approval_email(
            draft, source_refs, links, settings=settings, now=now
        )
        email_sent = _send_approval_email(
            mode, sender, settings, subject, text, html, send_deduper
        )
    except Exception as exc:  # noqa: BLE001 — a compose/send failure degrades the run
        _degrade("email", "approval email compose/send failed", exc)

    # -------------------------------------------------------------- FINALISE
    # _finalize commits again — recording the durable ``email_sent`` marker (the
    # outbox completion) before the best-effort degraded-run alert is sent.
    status = "partial" if alerts else "ok"
    return _finalize(
        session, run, status,
        mode=mode, sender=sender, settings=settings,
        draft_id=str(draft.id), email_sent=email_sent, alerts=alerts, stats=stats,
    )


def _default_send_deduper() -> SendDeduper:
    """Build the production send-deduper at an env-configurable state path.

    Keeps a same-day re-run of the cron idempotent from the owner's inbox (§14.5).
    The path is config-over-code (VISION_MAIL_STATE) so an operator can point it at
    a durable location; it defaults under the system temp dir for a bare checkout.
    """
    configured = os.environ.get("VISION_MAIL_STATE")
    state_path = Path(configured) if configured else Path(gettempdir()) / "vision" / "mail-dedup.json"
    return SendDeduper(state_path)


def main() -> int:
    """``vision-daily`` console entry point (cron ~06:30 IST, BRD §10.2).

    Configures logging, opens one transactional session, runs the pipeline for the
    configured mode, and returns a process exit code so cron can alert on failure.
    ``run_daily`` never raises for an operational failure; the outer guard catches
    only the truly unforeseen so a bad day exits non-zero rather than crash-looping.
    """
    configure_logging()
    settings = get_settings()
    mode = settings.vision_env
    logger.info("vision-daily invoked", extra={"env": mode.value})

    try:
        with get_session() as session:
            result = run_daily(
                datetime.now(timezone.utc),
                mode,
                session=session,
                settings=settings,
                send_deduper=_default_send_deduper(),
            )
    except Exception:
        # Last-resort boundary: the transaction rolled back, nothing half-applied.
        logger.exception("vision-daily crashed unexpectedly (fail-closed)")
        return 1

    logger.info(
        "vision-daily complete",
        extra={
            "status": result.status,
            "draft_id": result.draft_id,
            "email_sent": result.email_sent,
            "alerts": len(result.alerts),
        },
    )
    # ok/partial are both "the job ran"; ``skipped`` is a benign overlap (a sibling
    # is handling today). Only a total failure signals non-zero so cron alerts.
    return 0 if result.status in ("ok", "partial", "skipped") else 1


if __name__ == "__main__":
    raise SystemExit(main())

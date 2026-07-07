"""End-to-end INTEGRATION test for the DAILY ORCHESTRATION, fully offline.

WHY this test exists: the unit + phase suites prove each stage (ingest, curate,
synthesise, visuals, mailer, publish) in isolation. This proves the ``vision-daily``
GLUE (``vision.cli.daily.run_daily``) composes them into the one real journey the
cron lives every morning (BRD §10.2/§10.3, FR-01..09, FR-20):

    seed enabled sources (SQLite)
      -> INGEST (a MOCK FeedFetcher — no network) -> normalise -> persist items
      -> CURATE (real select_top)
      -> SYNTHESISE (a MOCK BrahmastraClient — no model, canned JSON)
      -> build a ``pending_approval`` draft (+ own-post dedup fold-in)
      -> compose + (mode-gated) send the approval email (a MOCK sender — no SMTP)
      -> close the run record

The ONLY collaborators mocked are the real-world side-effects a test must never
perform: feeds (network), Brahmastra (subprocess/model), the email sender (SMTP/
HTTP), and the image client (model). Everything security-critical — the DB writes,
the signed approval tokens, the state value — is REAL code (BRD §18/§22). Each
test follows AAA (Arrange -> Act -> Assert), one behaviour per test.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock

from sqlalchemy.orm import Session

from vision.brahmastra.image_client import BrahmastraImageClient
from vision.cli.daily import _send_approval_email, run_daily
from vision.config import Settings, SignatureMode, VisionEnv
from vision.db.models import Draft, Run, Source
from vision.ops.joblock import acquire_job_lock, release_job_lock
from vision.ingest.feeds import FeedHealth, FetchResult, RawItem

# --- Deterministic constants ------------------------------------------------
# A fixed reference "now" so recency scoring, focus rotation, and token expiry are
# reproducible across machines (no wall-clock flakiness).
_NOW: datetime = datetime(2026, 7, 6, 6, 30, 0, tzinfo=timezone.utc)


# --- Config-shaped Arrange helpers (not inline magic) -----------------------


def _settings(env: VisionEnv) -> Settings:
    """Pinned settings for a run mode, independent of any developer's ``.env``.

    Every knob the pipeline reads (lanes, grounding floor, palette, cutoff) is
    nailed down here so the orchestration is reproducible on any machine.
    """
    return Settings(
        VISION_ENV=env,
        TZ="Asia/Kolkata",
        MODEL_GENERATE="gemini",
        MODEL_CRITIQUE="codex",
        MODEL_VERIFY="claude",
        GROUNDING_MIN_PCT=100,
        APPROVE_CUTOFF_LOCAL="20:00",
        CARD_BRAND_PALETTE="navy=#0B1F3A;gold=#C9A24B",
        # OFF keeps any render independent of a watermark logo file on disk.
        POST_SIGNATURE_MODE=SignatureMode.OFF,
        SECRET_HMAC_KEY="daily-orchestration-test-hmac",  # noqa: S106 - test placeholder
    )


def _seed_sources(session: Session) -> None:
    """Seed two enabled sources, one per content lane (hc + ai).

    ``get_enabled_sources`` reads these, and ``_persist_items`` maps a fetched
    item back to its source BY NAME — so the mock feed items must carry these exact
    ``source_name`` values.
    """
    session.add_all(
        [
            Source(name="HC Feed", lane="hc", kind="rss", url="https://hc.test/feed",
                   authority_weight=0.9, enabled=True),
            Source(name="AI Feed", lane="ai", kind="rss", url="https://ai.test/feed",
                   authority_weight=0.85, enabled=True),
        ]
    )
    session.flush()


def _raw_item(source_name: str, lane: str, slug: str, *, hours_ago: int) -> RawItem:
    """Build one immutable RawItem as a mock fetcher would return it.

    A recent ``published_epoch`` keeps the item inside the default recency window so
    the curate scorer does not drop it.
    """
    published = (_NOW - timedelta(hours=hours_ago)).timestamp()
    return RawItem(
        source_name=source_name,
        lane=lane,
        kind="rss",
        title=f"{lane.upper()} signal about {slug}",
        url=f"https://example.test/{lane}/{slug}",
        summary=f"A concise, qualitative summary of {slug} developments.",
        published_epoch=published,
    )


class _FakeFetcher:
    """A drop-in double for :class:`FeedFetcher` returning a canned FetchResult.

    Structurally satisfies the single ``fetch_all`` call the orchestration makes,
    so no network is ever touched. The exact result (items + per-source health) is
    supplied per test, which is what lets one test model a healthy batch and another
    model a failing lane.
    """

    def __init__(self, result: FetchResult) -> None:
        self._result = result

    def fetch_all(self, sources: list[Any]) -> FetchResult:
        return self._result


class _FakeBrahmastra:
    """A double for :class:`BrahmastraClient` returning canned JSON per pass.

    Mirrors the real adapter's surface (generate/critique/verify) and disambiguates
    the image pass — which also rides ``critique`` — by the RAFT heading present in
    the rendered prompt ("IMAGE DECISION"), exactly as the phase-1 integration does.
    """

    def __init__(self, responses: dict[str, dict[str, Any]]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, str | None]] = []

    def generate(self, prompt: str, lane: str | None = None) -> dict[str, Any]:
        return self._serve("generate", lane)

    def critique(self, prompt: str, lane: str | None = None) -> dict[str, Any]:
        key = "image" if "IMAGE DECISION" in prompt else "critique"
        return self._serve(key, lane)

    def verify(self, prompt: str, lane: str | None = None) -> dict[str, Any]:
        return self._serve("verify", lane)

    def _serve(self, key: str, lane: str | None) -> dict[str, Any]:
        self.calls.append((key, lane))
        # A fresh dict per call so a consumer mutating a result cannot corrupt the
        # canned source (tests must not share mutable state).
        return copy.deepcopy(self._responses[key])


def _canned_responses(*, image_type: str = "none") -> dict[str, dict[str, Any]]:
    """Canned generate/critique/verify/image JSON that validates the strict schemas.

    Deliberately number-free and with empty claim lists so the run does NOT depend
    on the runtime-generated item UUIDs (the draft is created regardless of the
    grounding verdict — grounding only sets ``auto_eligible``). ``image_type`` lets
    a test opt into the concept-illustration render path.
    """
    hook = "A clear operational signal cut through the noise today."
    body = (
        "Health systems and AI teams both shipped something genuinely useful. "
        "The throughline is quiet, compounding leverage rather than spectacle."
    )
    takeaway = "Pick one high-friction workflow and pilot a single automation."
    hashtags = ["#HealthcareAI", "#Operations", "#DigitalHealth"]

    generate = {"hook": hook, "body": body, "takeaway": takeaway,
                "hashtags": hashtags, "claims": []}
    critique = {"revised": {"hook": hook, "body": body, "takeaway": takeaway,
                            "hashtags": hashtags, "claims": []},
                "change_log": ["Tightened the hook"], "voice_flags": []}
    verify = {"grounded": [], "unsupported": [],
              "revised_post": {"hook": hook, "body": body, "takeaway": takeaway,
                               "hashtags": hashtags},
              "grounding_pct": 0.0, "confidence": 0.8}
    image = {"image_type": image_type,
             "rationale": "The post is qualitative — no rendered figures required.",
             "card_spec": None,
             "illustration_prompt": (
                 "an abstract, calm network of soft nodes" if image_type != "none" else None
             )}
    return {"generate": generate, "critique": critique, "verify": verify, "image": image}


def _healthy_result() -> FetchResult:
    """A two-lane fetch where BOTH sources succeeded."""
    items = [
        _raw_item("HC Feed", "hc", "revenue-cycle", hours_ago=6),
        _raw_item("AI Feed", "ai", "clinical-notes", hours_ago=10),
    ]
    checked = _NOW
    health = {
        "HC Feed": FeedHealth(name="HC Feed", ok=True, count=1, checked_at=checked),
        "AI Feed": FeedHealth(name="AI Feed", ok=True, count=1, checked_at=checked),
    }
    return FetchResult(items=items, health=health)


# ---------------------------------------------------------------------------
# (1) Full run: EVERYTHING mocked -> a pending_approval draft + a run row.
# ---------------------------------------------------------------------------
def test_full_run_produces_pending_approval_draft_and_run(db_session: Session) -> None:
    # --- Arrange: sources + mock feeds/model/sender (no network, no model) ---
    settings = _settings(VisionEnv.LIVE)
    _seed_sources(db_session)
    fetcher = _FakeFetcher(_healthy_result())
    brahmastra = _FakeBrahmastra(_canned_responses())
    sender = Mock()
    sender.send.return_value = True

    # --- Act ----------------------------------------------------------------
    result = run_daily(
        _NOW, VisionEnv.LIVE,
        session=db_session, settings=settings,
        fetcher=fetcher, brahmastra=brahmastra, sender=sender,
    )

    # --- Assert: a clean run row was closed ---------------------------------
    assert result.status == "ok"
    runs = db_session.query(Run).all()
    assert len(runs) == 1
    assert runs[0].status == "ok"

    # --- Assert: exactly one draft, in the pending_approval state -----------
    drafts = db_session.query(Draft).all()
    assert len(drafts) == 1
    draft = drafts[0]
    assert draft.state == "pending_approval"
    assert result.draft_id == str(draft.id)
    assert draft.post_text and draft.post_text.strip()
    # The approval link's single-use key + expiry are persisted (never the raw token).
    assert draft.approve_token_hash
    assert draft.token_expires_at is not None
    # Own-post dedup verdict was folded into the quality report for the email.
    assert "dedup_vs_own_90d" in (draft.quality_report or {})

    # --- Assert: the approval email was mock-sent EXACTLY once (live mode) ---
    sender.send.assert_called_once()
    assert result.email_sent is True


# ---------------------------------------------------------------------------
# (2) dry_run: full pipeline, but NO email is sent (the safe default mode).
# ---------------------------------------------------------------------------
def test_dry_run_sends_no_email(db_session: Session) -> None:
    # --- Arrange ------------------------------------------------------------
    settings = _settings(VisionEnv.DRY_RUN)
    _seed_sources(db_session)
    fetcher = _FakeFetcher(_healthy_result())
    brahmastra = _FakeBrahmastra(_canned_responses())
    sender = Mock()

    # --- Act ----------------------------------------------------------------
    result = run_daily(
        _NOW, VisionEnv.DRY_RUN,
        session=db_session, settings=settings,
        fetcher=fetcher, brahmastra=brahmastra, sender=sender,
    )

    # --- Assert: the draft was still produced, but nothing was emailed ------
    assert result.status == "ok"
    draft = db_session.query(Draft).one()
    assert draft.state == "pending_approval"
    sender.send.assert_not_called()  # FR-20 dry_run: no email
    assert result.email_sent is False


# ---------------------------------------------------------------------------
# (3) A failing ingest lane still completes: partial run + alert + a draft.
# ---------------------------------------------------------------------------
def test_failing_ingest_lane_completes_partial_with_alert(db_session: Session) -> None:
    # --- Arrange: the AI feed fails; the HC feed still yields one item ------
    settings = _settings(VisionEnv.LIVE)
    _seed_sources(db_session)
    partial = FetchResult(
        items=[_raw_item("HC Feed", "hc", "revenue-cycle", hours_ago=6)],
        health={
            "HC Feed": FeedHealth(name="HC Feed", ok=True, count=1, checked_at=_NOW),
            "AI Feed": FeedHealth(name="AI Feed", ok=False, count=0, checked_at=_NOW,
                                  error="HTTP 403"),
        },
    )
    fetcher = _FakeFetcher(partial)
    brahmastra = _FakeBrahmastra(_canned_responses())
    sender = Mock()
    sender.send.return_value = True

    # --- Act ----------------------------------------------------------------
    result = run_daily(
        _NOW, VisionEnv.LIVE,
        session=db_session, settings=settings,
        fetcher=fetcher, brahmastra=brahmastra, sender=sender,
    )

    # --- Assert: the run degraded to partial, but still produced a draft ----
    assert result.status == "partial"
    assert any("ingest" in alert for alert in result.alerts)
    draft = db_session.query(Draft).one()
    assert draft.state == "pending_approval"  # the healthy lane still yielded a post
    run = db_session.query(Run).one()
    assert run.status == "partial"
    # A degraded run alerts the owner (and, in live mode, still sends the approval
    # email) — so the sender was invoked and did not crash the job.
    assert sender.send.called


# ---------------------------------------------------------------------------
# (4) Total ingest failure (no items) ends the run early as failed, no crash.
# ---------------------------------------------------------------------------
def test_empty_ingest_finalises_failed_without_crashing(db_session: Session) -> None:
    # --- Arrange: the fetcher returns nothing at all ------------------------
    settings = _settings(VisionEnv.LIVE)
    _seed_sources(db_session)
    fetcher = _FakeFetcher(FetchResult(items=[], health={}))
    brahmastra = _FakeBrahmastra(_canned_responses())
    sender = Mock()
    sender.send.return_value = True

    # --- Act ----------------------------------------------------------------
    result = run_daily(
        _NOW, VisionEnv.LIVE,
        session=db_session, settings=settings,
        fetcher=fetcher, brahmastra=brahmastra, sender=sender,
    )

    # --- Assert: failed run, NO draft, the model was never invoked ----------
    assert result.status == "failed"
    assert db_session.query(Draft).count() == 0
    assert db_session.query(Run).one().status == "failed"
    assert brahmastra.calls == []  # synthesis never ran without items


# ---------------------------------------------------------------------------
# (5) staging mode DOES email the owner (FR-20 mode difference from dry_run).
# ---------------------------------------------------------------------------
def test_staging_mode_emails_the_owner(db_session: Session) -> None:
    # --- Arrange ------------------------------------------------------------
    settings = _settings(VisionEnv.STAGING)
    _seed_sources(db_session)
    fetcher = _FakeFetcher(_healthy_result())
    brahmastra = _FakeBrahmastra(_canned_responses())
    sender = Mock()
    sender.send.return_value = True

    # --- Act ----------------------------------------------------------------
    result = run_daily(
        _NOW, VisionEnv.STAGING,
        session=db_session, settings=settings,
        fetcher=fetcher, brahmastra=brahmastra, sender=sender,
    )

    # --- Assert: staging sends the approval email (unlike dry_run) ----------
    assert result.status == "ok"
    sender.send.assert_called_once()
    assert result.email_sent is True


# ---------------------------------------------------------------------------
# (6) The concept-illustration image path renders + attaches an image file.
# ---------------------------------------------------------------------------
def test_concept_illustration_is_rendered_and_attached(
    db_session: Session, tmp_path: Path, monkeypatch: Any
) -> None:
    # --- Arrange: image lane asks for a (number-free) concept illustration --
    monkeypatch.setenv("VISION_IMAGE_DIR", str(tmp_path))
    settings = _settings(VisionEnv.LIVE)
    _seed_sources(db_session)
    fetcher = _FakeFetcher(_healthy_result())
    brahmastra = _FakeBrahmastra(_canned_responses(image_type="concept-illustration"))
    sender = Mock()
    sender.send.return_value = True
    # A spec-bound mock image client so no real diffusion model is ever called.
    image_client = MagicMock(spec=BrahmastraImageClient)
    image_client.illustrate.return_value = b"\x89PNG\r\n\x1a\n-fake-illustration-bytes"

    # --- Act ----------------------------------------------------------------
    result = run_daily(
        _NOW, VisionEnv.LIVE,
        session=db_session, settings=settings,
        fetcher=fetcher, brahmastra=brahmastra, sender=sender,
        image_client=image_client,
    )

    # --- Assert: the illustration was generated and attached to the draft ---
    assert result.status == "ok"
    image_client.illustrate.assert_called_once()
    draft = db_session.query(Draft).one()
    assert draft.image_type == "concept-illustration"
    assert draft.image_path is not None
    assert Path(draft.image_path).exists()
    assert draft.image_source == settings.image_model


# ---------------------------------------------------------------------------
# (7) SECURITY — commit-before-send: the draft + single-use tokens are DURABLY
# committed BEFORE the approval email leaves the building (threat model / §22.9).
# If the email could go out before the commit, a later commit failure would leave
# the owner holding live links to a rolled-back draft, and a retry would
# double-send + double-create. We prove ordering deterministically.
# ---------------------------------------------------------------------------
def test_approval_email_is_sent_only_after_durable_commit(
    db_session: Session, monkeypatch: Any, tmp_path: Path
) -> None:
    # --- Arrange ------------------------------------------------------------
    settings = _settings(VisionEnv.LIVE)
    _seed_sources(db_session)
    fetcher = _FakeFetcher(_healthy_result())
    brahmastra = _FakeBrahmastra(_canned_responses())

    # Record the exact interleaving of DB commits and email sends.
    events: list[str] = []
    real_commit = db_session.commit

    def _spy_commit() -> None:
        events.append("commit")
        real_commit()

    monkeypatch.setattr(db_session, "commit", _spy_commit)

    sender = Mock()

    def _send(*_a: Any, **_k: Any) -> bool:
        events.append("send")
        return True

    sender.send.side_effect = _send

    # --- Act ----------------------------------------------------------------
    result = run_daily(
        _NOW, VisionEnv.LIVE,
        session=db_session, settings=settings,
        fetcher=fetcher, brahmastra=brahmastra, sender=sender,
        lock_dir=tmp_path,
    )

    # --- Assert: a durable commit strictly precedes the outbound email -------
    assert "send" in events, "the approval email must be sent in live mode"
    assert "commit" in events, "run_daily must durably commit the draft itself"
    assert events.index("commit") < events.index("send"), (
        "commit-before-send violated: the email went out before the draft/tokens "
        "were durably committed"
    )
    # The draft's single-use token key is persisted (durable, actionable).
    draft = db_session.query(Draft).one()
    assert draft.approve_token_hash
    assert result.email_sent is True


# ---------------------------------------------------------------------------
# (8) SECURITY — a `mark_sent` bookkeeping failure must NOT flip the delivered
# verdict to False (which would drive a same-day retry DUPLICATE email). The send
# already succeeded; a dedup-marker error is logged, delivered stays True.
# ---------------------------------------------------------------------------
def test_mark_sent_failure_keeps_delivered_true_and_sends_once(
    db_session: Session,
) -> None:
    # --- Arrange: a sender that succeeds, a deduper whose mark_sent explodes --
    settings = _settings(VisionEnv.LIVE)
    sender = Mock()
    sender.send.return_value = True
    deduper = Mock()
    deduper.is_suppressed.return_value = False
    deduper.mark_sent.side_effect = RuntimeError("dedup state dir unwritable")

    # --- Act ----------------------------------------------------------------
    delivered = _send_approval_email(
        VisionEnv.LIVE, sender, settings, "VISION daily draft — X — 6 Jul 2026",
        "text", "<p>html</p>", deduper,
    )

    # --- Assert: delivered truth survives the bookkeeping failure ------------
    assert delivered is True  # the email really went out; do not report it unsent
    sender.send.assert_called_once()  # exactly one send — no duplicate


# ---------------------------------------------------------------------------
# (9) SECURITY — overlapping cron runs are prevented by an atomic per-job lock
# (threat model §4). A second run started while today's lock is held must NOT mint
# a second approvable draft / send a second email — it skips.
# ---------------------------------------------------------------------------
def test_second_run_is_blocked_while_job_lock_is_held(
    db_session: Session, tmp_path: Path
) -> None:
    # --- Arrange: simulate a sibling run already holding today's lock --------
    settings = _settings(VisionEnv.LIVE)
    _seed_sources(db_session)
    fetcher = _FakeFetcher(_healthy_result())
    brahmastra = _FakeBrahmastra(_canned_responses())
    sender = Mock()
    sender.send.return_value = True

    held = acquire_job_lock("vision-daily", _NOW, lock_dir=tmp_path)
    assert held is not None, "the test must be able to pre-acquire the lock"

    # --- Act ----------------------------------------------------------------
    try:
        result = run_daily(
            _NOW, VisionEnv.LIVE,
            session=db_session, settings=settings,
            fetcher=fetcher, brahmastra=brahmastra, sender=sender,
            lock_dir=tmp_path,
        )
    finally:
        release_job_lock(held)  # never poison later tests

    # --- Assert: the overlapping run skipped without any side effect ---------
    assert result.status == "skipped"
    assert result.draft_id is None
    assert db_session.query(Draft).count() == 0  # no overlapping draft minted
    sender.send.assert_not_called()  # and no overlapping approval email


# ---------------------------------------------------------------------------
# (10) SECURITY — a same-day RE-RUN (lock already released) reuses the existing
# pending draft via the idempotency key instead of minting a fresh draft + fresh
# single-use tokens — so the owner can never approve two drafts for one day.
# ---------------------------------------------------------------------------
def test_same_day_rerun_reuses_draft_and_mints_no_new_token(
    db_session: Session, tmp_path: Path
) -> None:
    # --- Arrange ------------------------------------------------------------
    settings = _settings(VisionEnv.LIVE)
    _seed_sources(db_session)
    brahmastra = _FakeBrahmastra(_canned_responses())
    sender = Mock()
    sender.send.return_value = True

    # --- Act: run the SAME day twice (fresh fetch each time, same content) ---
    first = run_daily(
        _NOW, VisionEnv.LIVE,
        session=db_session, settings=settings,
        fetcher=_FakeFetcher(_healthy_result()), brahmastra=brahmastra, sender=sender,
        lock_dir=tmp_path,
    )
    draft1 = db_session.query(Draft).one()
    hash1 = draft1.approve_token_hash

    second = run_daily(
        _NOW, VisionEnv.LIVE,
        session=db_session, settings=settings,
        fetcher=_FakeFetcher(_healthy_result()), brahmastra=brahmastra, sender=sender,
        lock_dir=tmp_path,
    )

    # --- Assert: exactly one approvable draft, reused, no new token ----------
    assert db_session.query(Draft).count() == 1  # no second draft minted
    assert second.draft_id == first.draft_id  # the re-run reused the same draft
    db_session.refresh(draft1)
    assert draft1.approve_token_hash == hash1  # NO second single-use token minted
    assert sender.send.call_count == 1  # the approval email went out exactly once


# ---------------------------------------------------------------------------
# (11) SECURITY — fail-closed FR-18 gate (§22.9). If the own-post dedup check
# cannot produce a trustworthy verdict (it raises), we must NOT emit an approvable
# email; the draft is left non-actionable (no token minted / sent).
# ---------------------------------------------------------------------------
def test_dedup_failure_withholds_approval_email_fail_closed(
    db_session: Session, monkeypatch: Any, tmp_path: Path
) -> None:
    # --- Arrange: force the own-post dedup check to fail --------------------
    settings = _settings(VisionEnv.LIVE)
    _seed_sources(db_session)
    fetcher = _FakeFetcher(_healthy_result())
    brahmastra = _FakeBrahmastra(_canned_responses())
    sender = Mock()
    sender.send.return_value = True

    def _boom(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise RuntimeError("own-post dedup backend unavailable")

    monkeypatch.setattr("vision.cli.daily.check_against_own", _boom)

    # --- Act ----------------------------------------------------------------
    result = run_daily(
        _NOW, VisionEnv.LIVE,
        session=db_session, settings=settings,
        fetcher=fetcher, brahmastra=brahmastra, sender=sender,
        lock_dir=tmp_path,
    )

    # --- Assert: no approval email; the draft is non-actionable -------------
    assert result.email_sent is False
    assert result.status == "partial"
    assert any("dedup" in alert for alert in result.alerts)
    # No APPROVAL email was composed/sent (a degraded-run alert may still go out).
    approval_subjects = [
        call.args[0] for call in sender.send.call_args_list if "daily draft" in call.args[0]
    ]
    assert approval_subjects == []
    draft = db_session.query(Draft).one()
    assert not draft.approve_token_hash  # no single-use token minted for a bad verdict

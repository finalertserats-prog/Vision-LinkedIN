"""End-to-end INTEGRATION test for VISION's Phase-2 APPROVAL LOOP, fully offline.

WHY this test exists: the unit suites prove each Phase-2 stage in isolation —
token crypto (``tokens.py``), the FastAPI endpoints (``test_approval_web.py``),
and the mailer (``test_mailer.py``). This test proves they *compose* into the one
real journey the owner lives every day (BRD §14.1-§14.3):

    seed a pending-approval draft (+ §14.4 quality_report)
      -> issue REAL signed approve/reject/edit links (tokens.py, no faked crypto)
      -> compose the approval email (subject/post/quality/sources/buttons/footer)
         and hand it to a MOCK EmailSender (no SMTP, no network)
      -> drive the REAL approval service via TestClient:
           GET /approve   -> shows a confirmation page, mutates NOTHING
           POST /approve  -> atomically consume nonce + transition new->scheduled
                             ("approved/queued") + publish EXACTLY ONCE
           replay POST    -> rejected generically, publishes NOTHING a 2nd time
           expired token  -> rejected, no state change, publisher untouched
           edit flow      -> replaces text and re-approves (publish once, edited)
      -> assert an append-only audit_log row exists for every transition.

The ONLY collaborators mocked are the two real-world side-effects Phase 2 must
never perform in a test: the email provider (a recording :class:`_MockSender`)
and LinkedIn publishing (a recording :class:`_RecordingPublisher` behind the
``service.PublisherPort``). Everything security-critical — HMAC signing/verify,
single-use nonce consumption, the compare-and-set transition — is the REAL code.
No network, no subprocess, no real email, no real LinkedIn (BRD §18/§22).

All assertions follow AAA (Arrange -> Act -> Assert), one behaviour per test.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from vision.approval import service
from vision.approval.service import PublishCall
from vision.approval.tokens import issue_token
from vision.approval.web import InMemoryRateLimiter, create_app
from vision.config import Settings
from vision.db.base import Base
from vision.db import models  # noqa: F401 — register tables on Base.metadata
from vision.db.models import AuditLog, Draft, Item, Run, UsedToken
from vision.mailer.composer import SourceRef, compose_approval_email

# A fixed test secret + TTL. Constants (not shared mutable state) so tests stay
# independent. The secret is non-default so the token crypto exercises a real key.
_SECRET = "phase2-integration-hmac-secret"
_TTL = 3600  # one hour — comfortably unexpired for the happy paths

# A pinned "now" so the composed subject/footer are deterministic across machines.
_NOW = datetime(2026, 7, 6, 12, 0, 0)

# The draft's post text — long enough to read as a real, grounded post so the
# email preview and the char-count are meaningful.
_POST_TEXT = (
    "Two operational signals stood out across healthcare and AI today. "
    "A 200-bed hospital cut claim denials by 18% after moving revenue-cycle "
    "checks to automation, and an open clinical-note model cleared 223 evaluation "
    "cases without the licence cost. Both are concrete wins a leader can act on."
)
_HASHTAGS = ["#HealthcareAI", "#RevenueCycle", "#DigitalHealth"]
_FOCUS = "Revenue-cycle management"


# --- Mock collaborators (the ONLY two things mocked) ------------------------
@dataclass
class _RecordingPublisher:
    """A :class:`service.PublisherPort` that records calls and does NO I/O.

    Lets the tests assert "published EXACTLY once with these args" without ever
    touching LinkedIn (Phase 2 keeps publishing mocked behind the port; the real
    implementation lands in Phase 3 with the identical signature).
    """

    calls: list[PublishCall] = field(default_factory=list)

    def publish(
        self,
        *,
        draft_id: str,
        text: str,
        image_path: str | None,
        scheduled_for: datetime | None,
        idempotency_key: str = "",
    ) -> str:
        self.calls.append(
            PublishCall(
                draft_id=draft_id,
                text=text,
                image_path=image_path,
                scheduled_for=scheduled_for,
                idempotency_key=idempotency_key,
            )
        )
        return f"mock:{draft_id}"


@dataclass(frozen=True)
class _SentEmail:
    """An immutable record of one email handed to the mock sender."""

    subject: str
    text: str
    html: str
    to: str | None


@dataclass
class _MockSender:
    """A ``mailer.sender.EmailSender``-shaped mock: records, never sends.

    Structurally satisfies the ``send(subject, text, html, to)`` surface so the
    composer output can be exercised end-to-end (subject/body/html captured) with
    zero SMTP or HTTP — the credential-bearing real providers are never imported.
    """

    sent: list[_SentEmail] = field(default_factory=list)

    def send(self, subject: str, text: str, html: str, to: str | None = None) -> bool:
        self.sent.append(_SentEmail(subject=subject, text=text, html=html, to=to))
        return True


# --- Hermetic app harness ---------------------------------------------------
@dataclass
class Harness:
    """Everything a test needs to drive the app + inspect the DB."""

    client: TestClient
    publisher: _RecordingPublisher
    sender: _MockSender
    sessionmaker: sessionmaker
    settings: Settings


@pytest.fixture
def harness() -> Iterator[Harness]:
    """Build the real approval app wired to a fresh in-memory DB + mocks.

    A single shared in-memory SQLite connection (``StaticPool``) is used so the
    app and the test observe the SAME database; the injected ``session_factory``
    mirrors ``db.session.get_session`` (commit-on-success, rollback-on-error),
    which is exactly what makes the endpoint's atomic transaction real under test.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)

    @contextmanager
    def session_factory() -> Iterator[Session]:
        session = TestSession()
        try:
            yield session
            session.commit()
        except Exception:
            # Roll the whole unit of work back so a failed action leaves no
            # partial state (fail-closed) — never a bare except that swallows.
            session.rollback()
            raise
        finally:
            session.close()

    settings = Settings(
        SECRET_HMAC_KEY=_SECRET,
        TZ="Asia/Kolkata",
        CARD_BRAND_PALETTE="navy=#0B1F3A;gold=#C9A24B",
    )
    publisher = _RecordingPublisher()
    app = create_app(
        settings=settings,
        publisher=publisher,
        session_factory=session_factory,
        # Generous limit so the functional flows never trip the rate limiter.
        rate_limiter=InMemoryRateLimiter(max_requests=1000, window_seconds=60),
    )
    client = TestClient(app)
    try:
        yield Harness(
            client=client,
            publisher=publisher,
            sender=_MockSender(),
            sessionmaker=TestSession,
            settings=settings,
        )
    finally:
        client.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


# --- Seeding + inspection helpers -------------------------------------------
def _quality_report() -> dict[str, object]:
    """Return a §14.4-shaped quality_report for the seeded pending draft.

    Exactly the eight BRD §14.4 keys so the composed email renders a real report
    (grounding %, confidence, flag chips) rather than the "unavailable" fallback.
    """
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


@dataclass(frozen=True)
class _Seeded:
    """A seeded pending-approval draft plus the source rows for its email."""

    draft: Draft  # detached ORM row (stable id, attributes eager-loaded)
    sources: tuple[SourceRef, ...]  # the SOURCES list the email will show
    expires_at: datetime  # the approval-link expiry stamped on the draft


def _seed_pending_draft(harness: Harness) -> _Seeded:
    """Seed a run, two grounded items, and ONE pending-approval draft.

    ``state='new'`` IS the pending-approval state in the §10.4 machine (awaiting
    the owner's decision). The draft carries a real §14.4 quality_report, its
    grounded ``source_item_ids``, and a ``token_expires_at`` (so the email footer
    shows a concrete expiry). Returns the detached draft + the source refs so the
    email compose step reads exactly the persisted content.
    """
    # The approval-link expiry is the same value the token issuer will compute;
    # stamp it now so the composed footer and the minted tokens agree.
    _t, _h, expires_at = issue_token("00000000-0000-0000-0000-000000000000", "approve", _TTL, _SECRET)

    session = harness.sessionmaker()
    try:
        run = Run(status="ok", notes="phase-2 approval-loop fixture")
        session.add(run)
        session.flush()

        items = [
            Item(
                run_id=run.id,
                lane="hc",
                title="Hospital cuts claim denials with revenue-cycle automation",
                url="https://example.test/hc/denials-automation",
                summary="A 200-bed hospital reduced claim denials by 18%.",
            ),
            Item(
                run_id=run.id,
                lane="ai",
                title="Open model matches prior systems on clinical-note summarisation",
                url="https://example.test/ai/clinical-notes-model",
                summary="An open model cleared 223 evaluation cases.",
            ),
        ]
        session.add_all(items)
        session.flush()

        draft = Draft(
            run_id=run.id,
            lane_focus=_FOCUS,
            post_text=_POST_TEXT,
            hashtags=list(_HASHTAGS),
            source_item_ids=[str(i.id) for i in items],
            quality_report=_quality_report(),
            confidence=0.9,
            state=service.STATE_NEW,  # pending approval (§10.4)
            token_expires_at=expires_at,
            image_type="none",
        )
        session.add(draft)
        session.commit()
        session.refresh(draft)
        # Detach with attributes loaded so the composer can read them after close.
        sources = tuple(SourceRef(title=i.title, url=i.url) for i in items)
        session.expunge(draft)
        return _Seeded(draft=draft, sources=sources, expires_at=expires_at)
    finally:
        session.close()


def _mint(draft: Draft, action: str, *, ttl: int = _TTL) -> str:
    """Mint a REAL signed token for ``draft`` + ``action`` (no faked crypto)."""
    token_str, _hash, _exp = issue_token(str(draft.id), action, ttl, _SECRET)
    return token_str


def _signed_links(draft: Draft) -> dict[str, str]:
    """Mint the four signed action links the approval email must carry.

    The composer requires all four keys (approve/post_now/edit/reject); each URL
    points at the GET confirmation route, matching how the real daily job builds
    the email. These are server-minted (never user input), so they are placed
    verbatim into the links map.
    """
    base = "https://vision.local"
    return {
        "approve": f"{base}/approve?token={_mint(draft, 'approve')}",
        "post_now": f"{base}/post-now?token={_mint(draft, 'post_now')}",
        "edit": f"{base}/edit?token={_mint(draft, 'edit')}",
        "reject": f"{base}/reject?token={_mint(draft, 'reject')}",
    }


def _draft_state(harness: Harness, draft_id: object) -> str:
    """Read the current persisted state of a draft."""
    session = harness.sessionmaker()
    try:
        return session.get(Draft, draft_id).state
    finally:
        session.close()


def _used_count(harness: Harness) -> int:
    """Count rows in the single-use nonce ledger (``used_tokens``)."""
    session = harness.sessionmaker()
    try:
        return session.query(UsedToken).count()
    finally:
        session.close()


def _audit_actions(harness: Harness, draft_id: object) -> list[str]:
    """Return the audit_log ``action`` values recorded for a draft, oldest first."""
    session = harness.sessionmaker()
    try:
        rows = (
            session.query(AuditLog)
            .filter(AuditLog.entity == "draft", AuditLog.entity_id == str(draft_id))
            .order_by(AuditLog.at.asc())
            .all()
        )
        return [row.action for row in rows]
    finally:
        session.close()


# ---------------------------------------------------------------------------
# (3) Compose the approval email with a MOCK sender (no real send).
# ---------------------------------------------------------------------------
def test_approval_email_composes_all_sections_and_mock_sends(harness: Harness) -> None:
    # --- Arrange: a pending draft + freshly-minted signed links -------------
    seeded = _seed_pending_draft(harness)
    links = _signed_links(seeded.draft)

    # --- Act: compose the email, then hand it to the MOCK sender ------------
    subject, text, html = compose_approval_email(
        seeded.draft,
        seeded.sources,
        links,
        settings=harness.settings,
        now=_NOW,
    )
    accepted = harness.sender.send(subject, text, html)

    # --- Assert: SUBJECT carries the daily-draft headline + focus ----------
    assert subject == f"VISION daily draft — {_FOCUS} — 6 Jul 2026"

    # --- Assert: POST text appears verbatim in both bodies -----------------
    assert _POST_TEXT in text
    assert "Proposed post" in html  # the §14.1 PROPOSED POST section label

    # --- Assert: QUALITY REPORT is rendered (grounding + confidence) -------
    assert "QUALITY REPORT" in text  # plain-text section header
    assert "Grounding: 100.0%" in text
    assert "Confidence: 0.9" in text
    assert "Quality report" in html
    assert "100.0%" in html  # grounding chip in the HTML body

    # --- Assert: SOURCES are listed (title + link for each grounded item) --
    for src in seeded.sources:
        assert src.title in text
        assert src.url in html

    # --- Assert: all four action BUTTONS present with their signed links ---
    # Labels are HTML-escaped in the body (the composer escapes the visible text),
    # so match the escaped forms — the "&" in the approve label becomes "&amp;".
    for label in ("Approve &amp; schedule 09:00", "Post now", "Edit", "Reject"):
        assert label in html
    for url in links.values():
        assert url in html  # each button hrefs its exact signed link

    # --- Assert: the EXPIRY FOOTER is present (leaked-link mitigation) ------
    assert "expire" in text.lower()
    assert "expire" in html.lower()

    # --- Assert: the send was MOCKED — recorded once, no real provider -----
    assert accepted is True
    assert len(harness.sender.sent) == 1
    assert harness.sender.sent[0].subject == subject
    assert not isinstance(harness.sender, service.NoopPublisher)  # a sender, not a publisher


# ---------------------------------------------------------------------------
# (4a) GET /approve shows a confirmation page and does NOT mutate.
# ---------------------------------------------------------------------------
def test_get_approve_shows_confirmation_without_mutating(harness: Harness) -> None:
    # --- Arrange ------------------------------------------------------------
    seeded = _seed_pending_draft(harness)
    token = _mint(seeded.draft, "approve")

    # --- Act ----------------------------------------------------------------
    response = harness.client.get("/approve", params={"token": token})

    # --- Assert: a confirmation page (POST form), and NOTHING changed -------
    assert response.status_code == 200
    assert "Confirm" in response.text
    assert "<form" in response.text and 'method="post"' in response.text.lower()
    assert _draft_state(harness, seeded.draft.id) == service.STATE_NEW
    assert _used_count(harness) == 0  # nonce NOT consumed on a GET
    assert harness.publisher.calls == []  # nothing published on a GET
    assert _audit_actions(harness, seeded.draft.id) == []  # no transition logged


# ---------------------------------------------------------------------------
# (4b) POST /approve consumes the nonce, transitions, publishes EXACTLY once,
#      logs an audit row; then a replay is rejected and does NOT publish again.
# ---------------------------------------------------------------------------
def test_post_approve_then_replay_is_rejected(harness: Harness) -> None:
    # --- Arrange ------------------------------------------------------------
    seeded = _seed_pending_draft(harness)
    token = _mint(seeded.draft, "approve")

    # --- Act 1: the genuine approval ---------------------------------------
    approved = harness.client.post("/approve", data={"token": token})

    # --- Assert 1: scheduled ("approved/queued"), consumed once, one publish -
    assert approved.status_code == 200
    assert "approved" in approved.text.lower()
    assert _draft_state(harness, seeded.draft.id) == service.STATE_SCHEDULED
    assert _used_count(harness) == 1
    assert len(harness.publisher.calls) == 1
    call = harness.publisher.calls[0]
    assert call.draft_id == str(seeded.draft.id)
    assert call.scheduled_for is not None  # approve ENQUEUES for the next slot
    assert _audit_actions(harness, seeded.draft.id) == ["approved"]

    # --- Act 2: replay the very same link ----------------------------------
    replay = harness.client.post("/approve", data={"token": token})

    # --- Assert 2: generic rejection, NO second publish, still one nonce ----
    assert replay.status_code == 400
    assert "no longer valid" in replay.text.lower()
    assert len(harness.publisher.calls) == 1  # publisher NOT called again
    assert _used_count(harness) == 1  # ledger unchanged
    assert _audit_actions(harness, seeded.draft.id) == ["approved"]  # no new row


# ---------------------------------------------------------------------------
# (4c) An expired token is rejected — no state change, publisher untouched.
# ---------------------------------------------------------------------------
def test_expired_token_is_rejected(harness: Harness) -> None:
    # --- Arrange: a token whose TTL already lapsed --------------------------
    seeded = _seed_pending_draft(harness)
    token = _mint(seeded.draft, "approve", ttl=-10)

    # --- Act ----------------------------------------------------------------
    response = harness.client.post("/approve", data={"token": token})

    # --- Assert: generic error, nothing mutated, publisher never called -----
    assert response.status_code == 400
    assert "no longer valid" in response.text.lower()
    assert _draft_state(harness, seeded.draft.id) == service.STATE_NEW
    assert _used_count(harness) == 0
    assert harness.publisher.calls == []
    assert _audit_actions(harness, seeded.draft.id) == []


# ---------------------------------------------------------------------------
# (4d) The EDIT flow updates the text and re-approves (publish once, edited).
# ---------------------------------------------------------------------------
def test_edit_flow_updates_text_and_reapproves(harness: Harness) -> None:
    # --- Arrange ------------------------------------------------------------
    seeded = _seed_pending_draft(harness)
    token = _mint(seeded.draft, "edit")
    new_text = (
        "An edited but still-grounded take: prior-authorisation turnaround fell "
        "from 72 hours to 24, and denial rework dropped alongside it. Measure the delta."
    )

    # --- Act ----------------------------------------------------------------
    response = harness.client.post(
        "/edit",
        data={"token": token, "post_text": new_text, "hashtags": "#Health #AI #RCM"},
    )

    # --- Assert: text replaced, scheduled, published ONCE with the EDITED copy
    assert response.status_code == 200
    assert _draft_state(harness, seeded.draft.id) == service.STATE_SCHEDULED
    assert _used_count(harness) == 1
    assert len(harness.publisher.calls) == 1
    assert harness.publisher.calls[0].text == new_text
    assert _audit_actions(harness, seeded.draft.id) == ["edited_approved"]

    # The persisted draft is exactly the edited revision (publish-only-approved).
    session = harness.sessionmaker()
    try:
        persisted = session.get(Draft, seeded.draft.id)
        assert persisted.post_text == new_text
        assert persisted.hashtags == ["#Health", "#AI", "#RCM"]
    finally:
        session.close()


# ---------------------------------------------------------------------------
# (4e) The REJECT flow discards the draft and NEVER publishes.
# ---------------------------------------------------------------------------
def test_reject_flow_discards_without_publishing(harness: Harness) -> None:
    # --- Arrange ------------------------------------------------------------
    seeded = _seed_pending_draft(harness)
    token = _mint(seeded.draft, "reject")

    # --- Act ----------------------------------------------------------------
    response = harness.client.post("/reject", data={"token": token})

    # --- Assert: rejected, nonce consumed, publisher NEVER called ----------
    assert response.status_code == 200
    assert "rejected" in response.text.lower()
    assert _draft_state(harness, seeded.draft.id) == service.STATE_REJECTED
    assert _used_count(harness) == 1
    assert harness.publisher.calls == []
    assert _audit_actions(harness, seeded.draft.id) == ["rejected"]


# ---------------------------------------------------------------------------
# Full-loop narrative: one draft, the whole §14 journey, audit trail intact.
# ---------------------------------------------------------------------------
def test_full_approval_loop_narrative(harness: Harness) -> None:
    """Compose -> GET (no mutate) -> POST approve (publish once) -> replay blocked.

    A single cohesive walk of the owner's real journey on ONE draft, asserting the
    audit_log is the single source of truth for what happened and that publishing
    happened exactly once across the whole narrative.
    """
    # --- Arrange: pending draft + email + signed approve link --------------
    seeded = _seed_pending_draft(harness)
    subject, _text, html = compose_approval_email(
        seeded.draft, seeded.sources, _signed_links(seeded.draft),
        settings=harness.settings, now=_NOW,
    )
    harness.sender.send(subject, _text, html)
    token = _mint(seeded.draft, "approve")

    # --- Act 1 + Assert: GET is inert --------------------------------------
    get_resp = harness.client.get("/approve", params={"token": token})
    assert get_resp.status_code == 200
    assert _draft_state(harness, seeded.draft.id) == service.STATE_NEW

    # --- Act 2 + Assert: POST approves, publishes once, logs the transition -
    post_resp = harness.client.post("/approve", data={"token": token})
    assert post_resp.status_code == 200
    assert _draft_state(harness, seeded.draft.id) == service.STATE_SCHEDULED
    assert len(harness.publisher.calls) == 1

    # --- Act 3 + Assert: replay is blocked, publish count unchanged --------
    replay_resp = harness.client.post("/approve", data={"token": token})
    assert replay_resp.status_code == 400
    assert len(harness.publisher.calls) == 1

    # --- Assert: the audit trail records exactly one approved transition ----
    assert _audit_actions(harness, seeded.draft.id) == ["approved"]
    assert len(harness.sender.sent) == 1  # the email was mock-sent exactly once

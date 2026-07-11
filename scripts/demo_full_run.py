"""Full-system DRY_RUN of Project VISION, end-to-end, with EVERYTHING external stubbed.

WHY this script exists: ``tests/test_daily_orchestration.py`` proves the daily glue
in a pytest harness. This is its runnable twin — a single command that drives the
REAL ``vision.cli.daily.run_daily`` orchestration through the whole morning journey
(ingest -> curate -> synthesise -> draft -> image -> own-dedup -> email -> run
record) while touching NO real-world side effect:

  * feeds     -> a canned ``FetchResult`` (no network),
  * models    -> a canned Brahmastra double (no CLI/model subprocess),
  * email     -> a spy sender that records calls (no SMTP/HTTP), and
  * database  -> a fresh in-memory SQLite engine (no ``vision.db`` file touched),
  * LinkedIn  -> never referenced (publishing is a SEPARATE process, ``vision-publisher``).

It runs in ``VISION_ENV=dry_run`` (FR-20 safe default) and then ASSERTS the contract
of a good daily run:

  1. exactly one ``pending_approval`` draft is produced,
  2. that draft carries a quality report,
  3. an informative card is rendered + attached (the optional visual),
  4. NO email is sent,
  5. NOTHING is posted (no post URN/URL; no own-post memory written),
  6. a durable ``runs`` record is written and closed.

Run:
    .venv/Scripts/python scripts/demo_full_run.py

Exit code is 0 only when every assertion holds, so CI/cron can gate on it.

Note on ``print``: operational logs go through ``logging`` (BRD §22). The PASS/FAIL
report block is written with ``print`` deliberately — it is this script's
user-facing deliverable, not debug output.
"""

from __future__ import annotations

import copy
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# Force the safe default mode BEFORE any settings singleton could be built, so even
# code paths that read the process env (rather than the injected Settings) see dry_run.
os.environ["VISION_ENV"] = "dry_run"

from vision.cli.daily import run_daily  # noqa: E402  (import after env is pinned)
from vision.config import Settings, SignatureMode, VisionEnv  # noqa: E402
from vision.db.base import Base  # noqa: E402
from vision.db import models  # noqa: E402, F401  (register tables on Base.metadata)
from vision.db.models import Draft, OwnPost, Run, Source  # noqa: E402
from vision.ingest.feeds import FeedHealth, FetchResult, RawItem  # noqa: E402
from vision.logging_setup import configure_logging, get_logger  # noqa: E402

logger: logging.Logger = get_logger("vision.scripts.demo_full_run")

# A fixed reference "now" so recency scoring, focus rotation, and token expiry are
# fully reproducible on any machine (no wall-clock flakiness).
_NOW: datetime = datetime(2026, 7, 6, 6, 30, 0, tzinfo=timezone.utc)

# Two fixture ids the canned model output is self-consistent about: the verifier
# grounds exactly these, and the informative card cites exactly these — so the
# pipeline's own "every card number traces to a grounded claim" gate (§13.6) passes
# without the script needing the DB's runtime item UUIDs.
_HC_ID = "demo-hc-1"
_AI_ID = "demo-ai-1"


# ---------------------------------------------------------------------------
# Hermetic settings + DB (no developer .env, no real database).
# ---------------------------------------------------------------------------
def _settings() -> Settings:
    """Pinned dry_run settings, independent of any local ``.env``."""
    return Settings(
        VISION_ENV=VisionEnv.DRY_RUN,
        TZ="Asia/Kolkata",
        MODEL_GENERATE="gemini",
        MODEL_CRITIQUE="codex",
        MODEL_VERIFY="claude",
        GROUNDING_MIN_PCT=100,
        APPROVE_CUTOFF_LOCAL="20:00",
        CARD_BRAND_PALETTE="navy=#0B1F3A;gold=#C9A24B",
        # OFF keeps the card render independent of any watermark logo file on disk.
        POST_SIGNATURE_MODE=SignatureMode.OFF,
        SECRET_HMAC_KEY="demo-full-run-hmac",  # noqa: S106 - hermetic demo placeholder
    )


def _in_memory_session() -> Session:
    """Build a fresh in-memory SQLite session with the full schema created.

    A dedicated engine (not the app's ``vision.db``) keeps the demo hermetic — it
    can never mutate a real database. ``StaticPool`` keeps the one in-memory
    connection alive for the whole run.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)()


def _seed_sources(session: Session) -> None:
    """Seed two enabled sources, one per content lane (hc + ai)."""
    session.add_all(
        [
            Source(name="HC Feed", lane="hc", kind="rss", url="https://hc.test/feed",
                   authority_weight=0.9, enabled=True),
            Source(name="AI Feed", lane="ai", kind="rss", url="https://ai.test/feed",
                   authority_weight=0.85, enabled=True),
        ]
    )
    session.flush()


# ---------------------------------------------------------------------------
# External-world doubles (no network, no model, no SMTP).
# ---------------------------------------------------------------------------
def _raw_item(source_name: str, lane: str, slug: str, *, hours_ago: int) -> RawItem:
    """One immutable RawItem as a mock fetcher would return it (recent enough to survive recency)."""
    return RawItem(
        source_name=source_name,
        lane=lane,
        kind="rss",
        title=f"{lane.upper()} signal about {slug}",
        url=f"https://example.test/{lane}/{slug}",
        summary=f"A concise, qualitative summary of {slug} developments.",
        published_epoch=(_NOW - timedelta(hours=hours_ago)).timestamp(),
    )


def _healthy_fetch_result() -> FetchResult:
    """A two-lane fetch where BOTH sources succeeded."""
    return FetchResult(
        items=[
            _raw_item("HC Feed", "hc", "revenue-cycle", hours_ago=6),
            _raw_item("AI Feed", "ai", "clinical-notes", hours_ago=10),
        ],
        health={
            "HC Feed": FeedHealth(name="HC Feed", ok=True, count=1, checked_at=_NOW),
            "AI Feed": FeedHealth(name="AI Feed", ok=True, count=1, checked_at=_NOW),
        },
    )


class _FakeFetcher:
    """Drop-in double for :class:`FeedFetcher` — returns a canned FetchResult, no network."""

    def __init__(self, result: FetchResult) -> None:
        self._result = result

    def fetch_all(self, sources: list[Any]) -> FetchResult:
        return self._result


class _FakeBrahmastra:
    """Double for :class:`BrahmastraClient` — canned JSON per pass, no model/subprocess.

    Disambiguates the image pass (which also rides ``critique``) by the RAFT
    "IMAGE DECISION" heading in the rendered prompt, exactly as the real chain does.
    """

    def __init__(self, responses: dict[str, dict[str, Any]]) -> None:
        self._responses = responses
        self.calls: list[str] = []

    def generate(self, prompt: str, lane: str | None = None) -> dict[str, Any]:
        return self._serve("generate")

    def critique(self, prompt: str, lane: str | None = None) -> dict[str, Any]:
        return self._serve("image" if "IMAGE DECISION" in prompt else "critique")

    def verify(self, prompt: str, lane: str | None = None) -> dict[str, Any]:
        return self._serve("verify")

    def _serve(self, key: str) -> dict[str, Any]:
        self.calls.append(key)
        # Fresh copy per call so a consumer mutating a result cannot corrupt the source.
        return copy.deepcopy(self._responses[key])


class _SpySender:
    """Email sender double that RECORDS every send but performs no I/O.

    The point of the demo is to PROVE dry_run sends nothing: if ``send`` is ever
    called, ``calls`` is non-empty and the assertion fails loudly.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def send(self, subject: str, text: str, html: str) -> bool:
        self.calls.append((subject, text, html))
        return True


def _canned_responses() -> dict[str, dict[str, Any]]:
    """Grounded generate/critique/verify/image JSON that validates the strict schemas.

    Self-consistent ids let the informative-card path render a REAL card: the
    verifier grounds ``_HC_ID``/``_AI_ID`` and the card cites the same, so the
    pipeline's card-grounding gate (§13.6) is satisfied.
    """
    hook = "Claim denials fell 18% at one hospital after revenue-cycle automation."
    body = (
        "Two operational signals stood out across the healthcare and AI lanes today. "
        "A 200-bed hospital cut claim denials by 18% after moving revenue-cycle checks "
        "to automation. In parallel, an open clinical-note model cleared 223 evaluation "
        "cases — matching prior systems without the licence cost. The common thread is "
        "unglamorous: put automation where the friction and the denials actually are, "
        "then measure the delta. Neither result is hype; both are the kind of concrete "
        "win a healthcare leader can pressure-test this week with a single small pilot."
    )
    takeaway = "Pilot one automation on your highest-denial payer this month and measure the delta."
    hashtags = ["#HealthcareAI", "#RevenueCycle", "#DigitalHealth"]

    claims = [
        {"text": "claim-denial reduction was 18%", "source_item_id": _HC_ID},
        {"text": "eval cases cleared was 223", "source_item_id": _AI_ID},
    ]
    generate = {"hook": hook, "body": body, "takeaway": takeaway,
                "hashtags": hashtags, "claims": claims}
    critique = {"revised": copy.deepcopy(generate),
                "change_log": ["Sharpened the hook", "Tightened the takeaway"],
                "voice_flags": []}
    grounded = [
        {"text": c["text"], "source_item_id": c["source_item_id"], "verbatim_ok": True}
        for c in claims
    ]
    verify = {
        "grounded": grounded,
        "unsupported": [],
        "revised_post": {"hook": hook, "body": body, "takeaway": takeaway, "hashtags": hashtags},
        "grounding_pct": 100.0,
        "confidence": 0.9,
    }
    image = {
        "image_type": "informative-card",
        "rationale": "Post centres on two concrete, comparable figures — render deterministically.",
        "card_spec": {
            "title": "Today's grounded signals",
            "datapoints": [
                {"label": "Claim-denial reduction", "value": "18%", "source_item_id": _HC_ID},
                {"label": "Eval cases cleared", "value": "223", "source_item_id": _AI_ID},
            ],
        },
        "illustration_prompt": None,
    }
    return {"generate": generate, "critique": critique, "verify": verify, "image": image}


# ---------------------------------------------------------------------------
# Assertions + report.
# ---------------------------------------------------------------------------
def _check(results: list[tuple[str, bool, str]], label: str, passed: bool, detail: str) -> None:
    """Append one check to the running report."""
    results.append((label, passed, detail))


def main() -> int:
    """Drive one full dry_run pipeline, assert its contract, print PASS/FAIL. Returns 0 on all-pass."""
    configure_logging()
    settings = _settings()
    session = _in_memory_session()
    image_dir = Path(tempfile.mkdtemp(prefix="vision_demo_full_run_"))
    os.environ["VISION_IMAGE_DIR"] = str(image_dir)

    _seed_sources(session)
    fetcher = _FakeFetcher(_healthy_fetch_result())
    brahmastra = _FakeBrahmastra(_canned_responses())
    sender = _SpySender()

    logger.info("starting full-system dry_run demo", extra={"image_dir": str(image_dir)})

    # --- Act: the REAL orchestration, every external collaborator injected as a fake.
    result = run_daily(
        _NOW,
        VisionEnv.DRY_RUN,
        session=session,
        settings=settings,
        fetcher=fetcher,
        brahmastra=brahmastra,
        sender=sender,  # type: ignore[arg-type]  # structural stand-in for EmailSender
    )
    session.commit()

    # --- Assert: gather the full contract of a good dry_run.
    drafts = session.query(Draft).all()
    runs = session.query(Run).all()
    own_posts = session.query(OwnPost).all()
    draft = drafts[0] if len(drafts) == 1 else None

    checks: list[tuple[str, bool, str]] = []

    _check(checks, "run finished ok", result.status == "ok", f"status={result.status}")
    _check(checks, "exactly one draft produced", len(drafts) == 1, f"drafts={len(drafts)}")

    if draft is not None:
        _check(checks, "draft is pending_approval",
               draft.state == "pending_approval", f"state={draft.state}")
        _check(checks, "draft has post text",
               bool(draft.post_text and draft.post_text.strip()),
               f"chars={len(draft.post_text or '')}")
        qr = draft.quality_report or {}
        _check(checks, "quality_report present + populated",
               bool(qr) and "grounding_pct" in qr,
               f"keys={sorted(qr)[:6]}")
        _check(checks, "own-dedup folded into quality_report",
               "dedup_vs_own_90d" in qr,
               f"dedup={qr.get('dedup_vs_own_90d')}")
        _check(checks, "approval token key + expiry persisted (not raw token)",
               bool(draft.approve_token_hash) and draft.token_expires_at is not None,
               f"hash_len={len(draft.approve_token_hash or '')}")
        # Optional visual: an informative card was rendered + attached deterministically.
        card_ok = (
            draft.image_type == "informative-card"
            and draft.image_path is not None
            and Path(draft.image_path).exists()
            and draft.image_source == "deterministic"
        )
        card_bytes = (
            Path(draft.image_path).stat().st_size if draft.image_path and Path(draft.image_path).exists() else 0
        )
        _check(checks, "informative card rendered + attached (optional visual)",
               card_ok, f"type={draft.image_type} bytes={card_bytes}")
        # Posts NOTHING: no publish artefacts on the draft.
        _check(checks, "nothing posted (no post URN/URL on draft)",
               draft.post_urn is None and draft.post_url is None,
               f"urn={draft.post_urn} url={draft.post_url}")

    # NO real email sent — neither the pipeline's return flag nor the spy saw a send.
    _check(checks, "NO email sent (dry_run)",
           result.email_sent is False and len(sender.calls) == 0,
           f"email_sent={result.email_sent} sender_calls={len(sender.calls)}")

    # Posts NOTHING: the daily job never writes own-post publish memory.
    _check(checks, "nothing posted (no own_post rows written)",
           len(own_posts) == 0, f"own_posts={len(own_posts)}")

    # A durable run record was written and closed.
    run_ok = (
        len(runs) == 1
        and runs[0].status == "ok"
        and bool(runs[0].stats)
        and runs[0].stats.get("mode") == "dry_run"
        and "finished_at" in (runs[0].stats or {})
    )
    _check(checks, "run record written + closed",
           run_ok,
           f"runs={len(runs)} status={runs[0].status if runs else None}")

    # Models ran (the pipeline really exercised synthesis), no network/SMTP touched.
    _check(checks, "synthesis passes actually executed",
           brahmastra.calls[:3] == ["generate", "critique", "verify"],
           f"calls={brahmastra.calls}")

    # --- Report ------------------------------------------------------------
    all_passed = all(ok for _, ok, _ in checks)
    line = "=" * 78
    print(line)
    print("PROJECT VISION — FULL-SYSTEM DRY_RUN (all external deps stubbed)")
    print(line)
    print(f"mode           : {settings.vision_env.value}")
    print(f"run_id         : {result.run_id}")
    print(f"run status     : {result.status}")
    print(f"draft_id       : {result.draft_id}")
    print(f"email_sent     : {result.email_sent}")
    print(f"alerts         : {list(result.alerts) or 'none'}")
    print(f"image dir      : {image_dir}")
    print("-" * 78)
    for label, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label:<52} {detail}")
    print(line)
    print(f"RESULT: {'PASS — all checks green' if all_passed else 'FAIL — see above'}")
    print(line)

    session.close()
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

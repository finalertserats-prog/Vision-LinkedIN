"""Model smoke tests — create one row per table and assert a clean round-trip.

WHY: proves the portable schema actually materialises on SQLite and that every
model's columns (including JSON arrays, LargeBinary, Uuid PKs and the §13.6 image
columns) persist and read back correctly. This is the acceptance gate for the
data layer scaffold.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from vision.db.models import (
    AuditLog,
    Draft,
    Item,
    OAuthToken,
    OwnPost,
    Run,
    Source,
    UsedToken,
)


def test_round_trip_one_row_per_table(db_session: Session) -> None:
    # --- Arrange: build one row per table, respecting FK dependencies -------
    # A run + source are parents for items/drafts, so create them first.
    run = Run(status="ok", stats={"items": 3, "tokens": 1200}, notes="smoke run")
    source = Source(
        name="STAT News",
        lane="hc",
        kind="rss",
        url="https://example.com/feed",
        authority_weight=0.9,
        enabled=True,
        last_ok_at=datetime.now(timezone.utc),
    )
    db_session.add_all([run, source])
    db_session.flush()  # assign PKs so children can reference them

    item = Item(
        source_id=source.id,
        run_id=run.id,
        lane="hc",
        title="A grounded headline",
        url="https://example.com/story",
        published_at=datetime.now(timezone.utc),
        summary="snippet",
        content_hash="abc123",
        relevance_score=0.75,
        selected=True,
    )
    draft = Draft(
        run_id=run.id,
        lane_focus="AI in clinical ops",
        post_text="Most 'AI in healthcare' wins are workflows.",
        hashtags=["#HealthTech", "#AIinHealthcare"],
        source_item_ids=[str(item.id)] if item.id else [],
        quality_report={"grounding_pct": 100, "confidence": 0.86},
        confidence=0.86,
        state="pending_approval",
        approve_token_hash="hmac-hash",
        image_type="informative-card",
        image_source="deterministic",
    )
    own_post = OwnPost(
        post_urn="urn:li:share:123",
        post_text="a prior post",
        embedding=[0.1, 0.2, 0.3],  # JSON list[float] portable embedding
        published_at=datetime.now(timezone.utc),
    )
    token = OAuthToken(
        provider="linkedin",
        access_token_enc=b"\x00\x01encrypted",  # LargeBinary round-trip
        refresh_token_enc=b"\x02\x03encrypted",
        member_urn="urn:li:person:abc",
    )
    audit = AuditLog(
        entity="draft",
        entity_id="some-id",
        action="approved",
        actor="owner",
        meta={"ip": "127.0.0.1"},
        at=datetime.now(timezone.utc),
    )
    used = UsedToken(nonce="nonce-xyz", action="approve", used_at=datetime.now(timezone.utc))

    db_session.add_all([item, draft, own_post, token, audit, used])

    # --- Act: commit the whole graph, then read each row back --------------
    db_session.commit()

    # --- Assert: one persisted row per table, key fields intact ------------
    assert db_session.execute(select(Source)).scalar_one().name == "STAT News"
    assert db_session.execute(select(Run)).scalar_one().status == "ok"

    fetched_item = db_session.execute(select(Item)).scalar_one()
    assert fetched_item.selected is True
    assert fetched_item.source_id == source.id

    fetched_draft = db_session.execute(select(Draft)).scalar_one()
    assert fetched_draft.hashtags == ["#HealthTech", "#AIinHealthcare"]  # JSON array round-trip
    assert fetched_draft.image_type == "informative-card"  # §13.6 column persists
    assert fetched_draft.quality_report["grounding_pct"] == 100  # JSON blob round-trip

    fetched_own = db_session.execute(select(OwnPost)).scalar_one()
    assert fetched_own.embedding == [0.1, 0.2, 0.3]  # portable vector round-trip

    fetched_token = db_session.execute(select(OAuthToken)).scalar_one()
    assert fetched_token.access_token_enc == b"\x00\x01encrypted"  # bytes survive

    assert db_session.execute(select(AuditLog)).scalar_one().action == "approved"
    assert db_session.execute(select(UsedToken)).scalar_one().nonce == "nonce-xyz"


def test_uuid_primary_keys_are_populated(db_session: Session) -> None:
    # Arrange / Act: a bare row should get a Python-side uuid4 PK on flush.
    run = Run(status="ok")
    db_session.add(run)
    db_session.flush()

    # Assert: the portable Uuid PK is generated without a DB-side function.
    assert run.id is not None
    assert run.created_at is not None  # server_default timestamp applied

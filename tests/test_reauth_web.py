"""Click-to-fix LinkedIn reconnect on vision-web (owner request 2026-11-21).

The email link opens /oauth/linkedin/start, which (only for a valid signed link)
sends the owner to LinkedIn; LinkedIn returns to /oauth/linkedin/callback, which
stores the new token. The callback must never accept a different LinkedIn member
than the one already connected, or a leaked link could hijack the posting account.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from vision.approval.web import InMemoryRateLimiter, create_app
from vision.config import Settings
from vision.db import models  # noqa: F401 - register tables on Base.metadata
from vision.db.base import Base
from vision.db.models import AlertState, OAuthToken
from vision.publish.reauth_link import issue_reauth_link_token

_SECRET = "unit-test-hmac-secret"  # noqa: S105 - test placeholder
_ENC_KEY = "unit-test-token-enc-key"  # noqa: S105 - test placeholder
_OWNER = "urn:li:person:OWNER"
_STRANGER = "urn:li:person:STRANGER"


@dataclass
class FakeLinkedIn:
    """Stands in for LinkedInClient: no network, records what was exchanged."""

    member_urn: str = _OWNER
    exchanged: list[str] = field(default_factory=list)

    def build_authorize_url(self, state: str) -> str:
        return f"https://linkedin.test/oauth?state={state}"

    def exchange_code(self, code: str) -> dict[str, object]:
        self.exchanged.append(code)
        return {"access_token": "fresh-access", "expires_in": 5_184_000}

    def get_member_urn(self, access_token: str) -> str:
        return self.member_urn

    def close(self) -> None:
        return None


@dataclass
class Harness:
    client: TestClient
    linkedin: FakeLinkedIn
    sessions: sessionmaker[Session]


@pytest.fixture
def harness() -> Iterator[Harness]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)

    @contextmanager
    def session_factory() -> Iterator[Session]:
        session = sessions()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    linkedin = FakeLinkedIn()
    app = create_app(
        settings=Settings(SECRET_HMAC_KEY=_SECRET, TOKEN_ENC_KEY=_ENC_KEY),
        session_factory=session_factory,
        rate_limiter=InMemoryRateLimiter(max_requests=1000, window_seconds=60),
        linkedin_client_factory=lambda: linkedin,
    )
    # https so the Secure state cookie is sent back, as in production.
    yield Harness(TestClient(app, base_url="https://testserver"), linkedin, sessions)
    engine.dispose()


def _seed_owner(harness: Harness) -> None:
    with harness.sessions() as session:
        session.add(OAuthToken(provider="linkedin", member_urn=_OWNER))
        session.add(
            AlertState(
                dedup_key="token_reauth_needed::LinkedIn re-authorisation required",
                last_fired_at=datetime.now(timezone.utc),
            )
        )
        session.commit()


def _start(harness: Harness) -> str:
    """Follow a valid email link; return the state LinkedIn was sent."""
    token = issue_reauth_link_token(_SECRET, datetime.now(timezone.utc))
    response = harness.client.get(
        "/oauth/linkedin/start", params={"t": token}, follow_redirects=False
    )
    assert response.status_code == 303
    return parse_qs(urlparse(response.headers["location"]).query)["state"][0]


def test_valid_link_redirects_to_linkedin_consent(harness: Harness) -> None:
    token = issue_reauth_link_token(_SECRET, datetime.now(timezone.utc))
    response = harness.client.get(
        "/oauth/linkedin/start", params={"t": token}, follow_redirects=False
    )
    assert response.headers["location"].startswith("https://linkedin.test/oauth")


def test_start_sets_a_locked_down_state_cookie(harness: Harness) -> None:
    token = issue_reauth_link_token(_SECRET, datetime.now(timezone.utc))
    response = harness.client.get(
        "/oauth/linkedin/start", params={"t": token}, follow_redirects=False
    )
    cookie = response.headers["set-cookie"].lower()
    assert all(flag in cookie for flag in ("httponly", "secure", "samesite=lax"))


def test_expired_link_is_refused(harness: Harness) -> None:
    old = datetime.now(timezone.utc) - timedelta(days=30)
    token = issue_reauth_link_token(_SECRET, old, ttl=timedelta(days=14))
    response = harness.client.get(
        "/oauth/linkedin/start", params={"t": token}, follow_redirects=False
    )
    assert response.status_code == 400


def test_start_without_a_link_is_refused(harness: Harness) -> None:
    response = harness.client.get("/oauth/linkedin/start", follow_redirects=False)
    assert response.status_code == 400


def test_owner_reconnect_stores_the_new_token(harness: Harness) -> None:
    _seed_owner(harness)
    state = _start(harness)

    response = harness.client.get(
        "/oauth/linkedin/callback", params={"code": "c1", "state": state}
    )

    assert response.status_code == 200
    with harness.sessions() as session:
        row = session.scalars(select(OAuthToken)).one()
        assert row.access_token_enc is not None


def test_reconnect_rearms_the_single_alert_email(harness: Harness) -> None:
    _seed_owner(harness)
    state = _start(harness)

    harness.client.get("/oauth/linkedin/callback", params={"code": "c1", "state": state})

    with harness.sessions() as session:
        assert session.scalars(select(AlertState)).all() == []


def test_a_different_linkedin_member_is_refused(harness: Harness) -> None:
    _seed_owner(harness)
    harness.linkedin.member_urn = _STRANGER
    state = _start(harness)

    response = harness.client.get(
        "/oauth/linkedin/callback", params={"code": "c1", "state": state}
    )

    assert response.status_code == 400
    with harness.sessions() as session:
        assert [r.member_urn for r in session.scalars(select(OAuthToken))] == [_OWNER]


def test_callback_without_the_state_cookie_never_spends_the_code(harness: Harness) -> None:
    _seed_owner(harness)

    response = harness.client.get(
        "/oauth/linkedin/callback", params={"code": "c1", "state": "forged"}
    )

    assert response.status_code == 400
    assert harness.linkedin.exchanged == []


def test_callback_with_a_mismatched_state_never_spends_the_code(harness: Harness) -> None:
    _seed_owner(harness)
    _start(harness)

    harness.client.get("/oauth/linkedin/callback", params={"code": "c1", "state": "forged"})

    assert harness.linkedin.exchanged == []


def test_web_reconnect_is_refused_when_no_account_is_connected(harness: Harness) -> None:
    # A first-time connection must go through the operator script, never a link.
    state = _start(harness)

    response = harness.client.get(
        "/oauth/linkedin/callback", params={"code": "c1", "state": state}
    )

    assert response.status_code == 400


def test_failed_callback_clears_the_state_cookie(harness: Harness) -> None:
    _seed_owner(harness)
    harness.linkedin.member_urn = _STRANGER
    state = _start(harness)

    response = harness.client.get(
        "/oauth/linkedin/callback", params={"code": "c1", "state": state}
    )

    assert "vision_li_oauth_state=" in response.headers.get("set-cookie", "")

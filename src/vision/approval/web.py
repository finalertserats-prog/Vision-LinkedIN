"""FastAPI approval service ``vision-web`` — signed action links (BRD §14.2/§14.3).

WHY this module exists: the daily email contains four one-click links —
Approve / Reject / Edit / Post-now — each carrying a signed, single-use,
expiring token. This app turns those links into safe state changes on the owner's
LinkedIn draft, under the discipline of the Codex threat model
(``prep/security_threatmodel.md``).

The single most important rule, and the shape of every route pair below:

    GET  /approve  → VERIFY the token (signature + expiry + single-use), then
                     render a CONFIRMATION page with a POST form. It NEVER mutates
                     — so an email scanner / link preview that fires a GET cannot
                     approve, reject, edit, or publish anything.
    POST /approve  → RE-VERIFY, then ATOMICALLY consume the single-use nonce
                     together with the state transition (compare-and-set), and
                     call the publisher exactly once.

Layered defences wired here (threat model §1/§2):
  * Security-headers middleware — ``Referrer-Policy: no-referrer`` so the token
    never leaks in a Referer header, plus nosniff / frame-deny / a strict CSP /
    ``Cache-Control: no-store``.
  * No public API docs — ``/docs``, ``/redoc`` and the OpenAPI schema are
    disabled so the endpoint surface is not advertised.
  * Restrictive CORS — no cross-origin access is granted.
  * Per-IP + per-token rate limiting — bounds token-validation flooding.
  * Generic error page — every failure (expired / replayed / tampered / unknown)
    renders the SAME "link no longer valid" page; the concrete reason is logged,
    never shown.
  * Fail-closed — any ambiguity raises and yields the generic error, never a
    partial action.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import FastAPI, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from vision.approval import edit_page, service, tokens
from vision.approval.errors import ApprovalTokenError
from vision.approval.tokens import VerifiedToken
from vision.config import Settings, get_settings
from vision.db.session import get_session

_log = logging.getLogger(__name__)

# Map a URL path segment to the canonical action word inside the signed token.
# The token action for "post now" is ``post_now`` while the URL is ``/post-now``
# (hyphen is the URL convention, underscore the token convention). Binding them
# here means a link's path and its signed action must AGREE or it is rejected.
_PATH_TO_ACTION: dict[str, str] = {
    "approve": "approve",
    "reject": "reject",
    "edit": "edit",
    "post-now": "post_now",
    # 'overrule' is a COUNCIL-only action wired as an EDIT-flow VARIANT (BRD §5):
    # the owner supplies a one-line counter-take that overrides the council's
    # synthesised post. It reuses the edit page + edit_apply machinery verbatim
    # (no new endpoint) — the only difference is the labelled prompt shown on GET.
    "overrule": "overrule",
}

# Actions that are handled through the EDIT machinery (same edit page + apply
# path). Grouping them here keeps the "overrule is an edit variant" decision in one
# auditable place rather than scattered ``action in {...}`` checks.
_EDIT_LIKE_ACTIONS: frozenset[str] = frozenset({"edit", "overrule"})

# The prompt shown on the overrule edit page so the owner knows this is an
# override, not an ordinary edit (labelled per BRD §5 / task item 2).
_OVERRULE_PROMPT = "Add your override:"

# A session factory is any zero-arg callable returning a context manager that
# yields a Session (commit-on-success). Prod uses ``get_session``; tests inject
# an in-memory-DB-backed factory of the same shape.
SessionFactory = Callable[[], AbstractContextManager[Session]]


# --- Rate limiting ----------------------------------------------------------
@dataclass
class InMemoryRateLimiter:
    """A minimal fixed-window per-key rate limiter (threat model §1 DoS).

    Keyed on ``ip:token-prefix`` so both a flooding IP and a hammered token are
    bounded (the hardening checklist asks for "per IP … and token/nonce" limits).
    In-memory by design for a single-instance self-hosted deploy; a Redis-backed
    limiter with the same ``allow`` method can be injected for a multi-instance
    deploy without touching the routes ("redis-optional").
    """

    max_requests: int = 30  # requests permitted per window per key
    window_seconds: float = 60.0  # rolling window length
    _hits: dict[str, list[float]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def allow(self, key: str) -> bool:
        """Return True if ``key`` is under its quota; record this hit if so."""
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            # Drop timestamps outside the window, then decide on the remainder.
            recent = [t for t in self._hits.get(key, ()) if t >= cutoff]
            if len(recent) >= self.max_requests:
                # Persist the pruned list so a blocked key still ages out.
                self._hits[key] = recent
                return False
            recent.append(now)
            self._hits[key] = recent
            return True


def _client_key(request: Request, token: str) -> str:
    """Build the rate-limit key from the DIRECT peer IP + a token prefix.

    Deliberately uses ``request.client.host`` (the direct socket peer) and NOT
    ``X-Forwarded-For`` — the threat model says forwarded headers must not be
    trusted unless they come from an allowlisted reverse proxy, which is a deploy
    concern, so the safe default here is the real peer. Only a short token prefix
    is used so the key never embeds a full token.
    """
    ip = request.client.host if request.client else "unknown"
    return f"{ip}:{token[:12]}"


def create_app(
    *,
    settings: Settings | None = None,
    publisher: service.PublisherPort | None = None,
    session_factory: SessionFactory | None = None,
    rate_limiter: InMemoryRateLimiter | None = None,
) -> FastAPI:
    """Build and return the configured ``vision-web`` FastAPI application.

    Everything the routes depend on (settings, publisher, DB sessions, limiter)
    is injected here so the app is trivially testable with a mock publisher and an
    in-memory database, and so production wiring stays declarative. Defaults
    resolve to the real singletons / a Phase-2 :class:`NoopPublisher`.
    """
    app_settings = settings or get_settings()
    app_publisher = publisher or service.NoopPublisher()
    app_sessions: SessionFactory = session_factory or get_session
    limiter = rate_limiter or InMemoryRateLimiter()
    palette = edit_page.build_palette(app_settings)

    # Public API docs are disabled in every environment: the threat model calls
    # for "no public docs", and there is nothing for a human to explore here —
    # the only clients are signed email links.
    app = FastAPI(
        title="vision-web",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # Restrictive CORS: no cross-origin site may script these endpoints. An empty
    # allowlist means the browser withholds CORS approval for any Origin.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=[],
    )

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Attach hardening headers to every response (threat model §1/§2).

        ``Referrer-Policy: no-referrer`` is the load-bearing one — it stops the
        signed token in the URL from leaking to any downstream via the Referer
        header. The CSP is deliberately tight but allows inline style/script
        because the pages are fully self-contained (no third-party assets) and the
        edit page uses a small inline counter script.
        """
        response = await call_next(request)
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; form-action 'self'; base-uri 'none'"
        )
        return response

    # --- Shared helpers (closures over the injected dependencies) ----------
    def _error_response(status_code: int = 400) -> HTMLResponse:
        """Return the single generic error page (never leaks the reason)."""
        return HTMLResponse(
            edit_page.render_error_page(palette=palette), status_code=status_code
        )

    def _safe_verify(token: str, session: Session) -> VerifiedToken | None:
        """Verify a token against the DB single-use ledger, or return None.

        Wraps :func:`tokens.verify_token` so every failure mode (invalid, expired,
        replayed, bad action) collapses to a single ``None`` at the route level —
        the route then renders the generic page. The concrete exception type is
        logged for the audit trail but never surfaced to the client.
        """
        if not token:
            return None
        try:
            return tokens.verify_token(
                token,
                app_settings.secret_hmac_key,
                datetime.now(timezone.utc),
                service.make_is_used(session),
            )
        except ApprovalTokenError as exc:
            # Log the CLASS (Expired/Used/Invalid/BadAction) — useful for ops —
            # but the response stays generic. No token value is logged.
            _log.info("token rejected: %s", type(exc).__name__)
            return None

    def _resolve_action(path_action: str) -> str | None:
        """Map a URL segment to a token action, or None if unknown."""
        return _PATH_TO_ACTION.get(path_action)

    def _dispatch_post(
        session: Session,
        *,
        action: str,
        verified: VerifiedToken,
        request: Request,
        post_text: str | None,
        hashtags_raw: str | None,
    ) -> service.ActionResult:
        """Route a verified POST to the matching service action."""
        actor_ip = request.client.host if request.client else None
        if action == "approve":
            return service.approve(
                session,
                verified=verified,
                settings=app_settings,
                publisher=app_publisher,
                actor_ip=actor_ip,
            )
        if action == "post_now":
            return service.post_now(
                session,
                verified=verified,
                settings=app_settings,
                publisher=app_publisher,
                actor_ip=actor_ip,
            )
        if action == "reject":
            return service.reject(
                session,
                verified=verified,
                settings=app_settings,
                actor_ip=actor_ip,
            )
        # action in {edit, overrule}: BOTH parse the edited/override fields from the
        # form and apply through the SAME edit machinery — an overrule is just an
        # edit whose new text is the owner's counter-take (no separate service call).
        return service.edit_apply(
            session,
            verified=verified,
            settings=app_settings,
            publisher=app_publisher,
            new_post_text=post_text or "",
            new_hashtags=_parse_hashtags(hashtags_raw),
            actor_ip=actor_ip,
        )

    # --- Routes ------------------------------------------------------------
    @app.get("/healthz")
    def healthz() -> JSONResponse:
        """Liveness/readiness probe: pipeline + DB + token-secret status.

        Returns 200 when the DB answers and the app is configured; 503 if the DB
        is unreachable (fail-closed for readiness). The body is intentionally
        terse and secret-free — it reports whether the HMAC secret is still the
        insecure dev default, but never the value.
        """
        db_ok = False
        try:
            with app_sessions() as session:
                session.execute(sql_text("SELECT 1"))
            db_ok = True
        except Exception as exc:  # noqa: BLE001 — health must report, not raise
            _log.warning("healthz DB check failed: %s", type(exc).__name__)
        secret_ok = app_settings.secret_hmac_key != "dev-insecure-hmac-key"
        payload = {
            "status": "ok" if db_ok else "degraded",
            "pipeline": "ok",  # no long-running pipeline in the web tier (Phase 2)
            "db": "ok" if db_ok else "error",
            "token_secret": "configured" if secret_ok else "insecure-default",
            "env": app_settings.vision_env.value,
        }
        return JSONResponse(payload, status_code=200 if db_ok else 503)

    @app.get("/{path_action}", response_class=HTMLResponse)
    def show_confirmation(path_action: str, request: Request, token: str = "") -> Response:
        """GET handler: verify WITHOUT consuming, then show a confirmation page.

        This route NEVER changes state (the threat-model invariant). For ``edit``
        it renders the editable page; for the others it renders a confirmation
        page whose only control is a POST form.
        """
        action = _resolve_action(path_action)
        if action is None:
            # Unknown path — same generic page (don't confirm which paths exist).
            return _error_response(404)
        if not limiter.allow(_client_key(request, token)):
            return _error_response(429)

        # A read-only session: verify (single-use check reads used_tokens) and
        # load the draft for display. No writes happen on GET.
        with app_sessions() as session:
            verified = _safe_verify(token, session)
            # The path's action must match the token's signed action, else this
            # is a mismatched/forged link → generic error.
            if verified is None or verified.action != action:
                return _error_response(400)
            try:
                draft = service.load_draft(session, verified.draft_id)
            except service.DraftNotFound:
                return _error_response(400)
            # Snapshot the fields we render BEFORE the session context closes, so
            # the response never lazy-loads on a closed session.
            post_text = draft.post_text or ""
            hashtags = list(draft.hashtags or [])

        action_url = f"/{path_action}"
        if action in _EDIT_LIKE_ACTIONS:
            # Overrule reuses the edit page verbatim; only the labelled prompt
            # differs so the owner knows this edit overrides the council's post.
            page = edit_page.render_edit_page(
                post_text=post_text,
                hashtags=hashtags,
                token=token,
                action_url=action_url,
                errors=None,
                prompt=_OVERRULE_PROMPT if action == "overrule" else None,
                palette=palette,
            )
            return HTMLResponse(page)

        # Confirmation page for approve / reject / post_now.
        labels = {
            "approve": ("Approve", "It will be scheduled for the next publish slot."),
            "reject": ("Reject", "The draft will be discarded."),
            "post_now": ("Post now", "It will be published immediately."),
        }
        label, note = labels[action]
        page = edit_page.render_confirmation_page(
            action=action,
            action_label=label,
            post_text=post_text,
            hashtags=hashtags,
            token=token,
            action_url=action_url,
            extra_note=note,
            palette=palette,
        )
        return HTMLResponse(page)

    @app.post("/{path_action}", response_class=HTMLResponse)
    def perform_action(
        path_action: str,
        request: Request,
        token: str = Form(default=""),
        post_text: str | None = Form(default=None),
        hashtags: str | None = Form(default=None),
    ) -> Response:
        """POST handler: re-verify, atomically consume + transition, publish once.

        The entire unit of work runs inside one session context so the state
        change and the nonce consumption commit together (or roll back together
        on ANY error — fail-closed). Every failure renders the generic error page;
        an invalid EDIT is the one exception, re-rendering the edit page with the
        specific validation problems (without consuming the token).
        """
        action = _resolve_action(path_action)
        if action is None:
            return _error_response(404)
        if not limiter.allow(_client_key(request, token)):
            return _error_response(429)

        try:
            with app_sessions() as session:
                verified = _safe_verify(token, session)
                if verified is None or verified.action != action:
                    # Not a ServiceError, so handle inline (context still open).
                    return _error_response(400)
                result = _dispatch_post(
                    session,
                    action=action,
                    verified=verified,
                    request=request,
                    post_text=post_text,
                    hashtags_raw=hashtags,
                )
            # Success: the context committed the atomic transition.
            return HTMLResponse(
                edit_page.render_result_page(
                    heading=result.heading, message=result.message, palette=palette
                )
            )
        except service.ValidationFailed as exc:
            # Invalid hand-edit: re-render the edit page WITH the problems so the
            # owner can fix and resubmit. The token was not consumed (validation
            # runs before consume), and the rollback undid nothing of substance.
            _log.info("edit validation failed: %d problem(s)", len(exc.problems))
            return HTMLResponse(
                edit_page.render_edit_page(
                    post_text=post_text or "",
                    hashtags=_parse_hashtags(hashtags),
                    token=token,
                    action_url=f"/{path_action}",
                    errors=exc.problems,
                    prompt=_OVERRULE_PROMPT if action == "overrule" else None,
                    palette=palette,
                ),
                status_code=400,
            )
        except service.ServiceError as exc:
            # StateConflict / ReplayDetected / DraftNotFound → generic page. The
            # session context already rolled back, so no partial state remains.
            _log.info("action rejected: %s", type(exc).__name__)
            return _error_response(400)

    return app


def _parse_hashtags(raw: str | None) -> list[str]:
    """Split the edit form's hashtag field into a clean list of tags.

    The edit page submits hashtags as one whitespace-separated string. We split on
    any whitespace and drop blanks; validation (well-formedness, count) is the
    validator's job, so this stays a pure, forgiving tokenizer.
    """
    if not raw:
        return []
    return [tag for tag in raw.split() if tag]


# NOTE: no module-level ``app = create_app()`` is defined on purpose. Building the
# app lazily (via the factory) keeps import side-effect-free and lets the ASGI
# entry point / tests construct it with the right injected dependencies. A
# deployment points uvicorn at a thin wrapper that calls ``create_app()``.

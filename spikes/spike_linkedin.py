"""spike_linkedin.py — de-risk the LinkedIn ``w_member_social`` publish path.

WHY this spike exists (BRD §20 Phase 0, §6/§15): before building the real
publisher we prove — against the LIVE LinkedIn API, once — that the whole
self-serve posting lifecycle actually works for the owner's *personal* profile:

    3-legged OAuth  ->  code exchange  ->  member-URN discovery
                    ->  publish a "hello world" test post  ->  delete it

It is a THROWAWAY, interactive probe (not production code): it drives the same
``vision.publish.linkedin.LinkedInClient`` the real worker uses, so a green run
here means the client + app config + scopes are correct end-to-end.

DO NOT run this in CI or unattended: it opens a real browser consent flow and
touches the live API. It is deliberately pinned to ``staging`` mode so the test
post is *published and then immediately deleted* (LinkedIn has no native draft
state — §6), leaving nothing lingering on the profile.

----------------------------------------------------------------------------
ONE-TIME SETUP (BRD §15.1 — do this once, by hand, before the first run)
----------------------------------------------------------------------------
  1. Create a LinkedIn developer app at https://www.linkedin.com/developers/apps
     and associate it with a Company Page you administer (required to request
     products, even for personal-profile posting).
  2. Request the **"Share on LinkedIn"** and **"Sign In with LinkedIn using
     OpenID Connect"** products so the app is granted the
     ``w_member_social openid profile email`` scopes (§16 least-privilege).
  3. Under *Auth*, add an **Authorized redirect URL** that EXACTLY matches
     ``LI_REDIRECT_URI`` in your ``.env`` (e.g. the localhost callback used for
     this spike). A mismatch is the most common cause of an OAuth failure.
  4. Copy the app's **Client ID** and **Client Secret** into ``.env`` as
     ``LI_CLIENT_ID`` / ``LI_CLIENT_SECRET`` (see ``.env.example``). The secret
     is read via ``vision.config`` and is NEVER logged or placed on a CLI arg.
  5. Set ``VISION_ENV=staging`` in ``.env`` so this spike self-deletes its post.

Then run it manually from the repo root:

    python spikes/spike_linkedin.py

It will print an authorize URL to open in your browser; after you consent,
LinkedIn redirects to your ``LI_REDIRECT_URI`` with ``?code=...&state=...``.
Paste that FULL redirected URL back at the prompt and the spike finishes the
round-trip. (The authorization ``code`` is single-use and short-lived; it is
treated as a secret and never logged.)

SECURITY (BRD §22, threat model §3): the access token, refresh token and the
authorization code are held only in local variables for the shortest possible
time and are NEVER written to a log line or passed as a CLI argument. Only
non-secret status (the member URN, the created post URN) is logged.
"""

from __future__ import annotations

import secrets
import sys
from urllib.parse import parse_qs, urlparse

from vision.config import Settings, VisionEnv, get_settings
from vision.logging_setup import configure_logging, get_logger
from vision.publish.errors import LinkedInError
from vision.publish.linkedin import LinkedInClient

_log = get_logger("spikes.spike_linkedin")

# The marker makes a stray post unmistakable if a delete ever fails — the same
# convention the real worker uses in staging (§15). It is content, not a secret.
_TEST_POST_TEXT = (
    "hello world — Project VISION LinkedIn spike (staging test post; "
    "this is auto-deleted immediately)."
)


class SpikeConfigError(RuntimeError):
    """Raised when the environment is not safe/complete enough to run the spike.

    A dedicated type (never a bare ``raise``) lets ``main`` map a mis-configured
    run onto a clean, non-zero exit with an actionable message instead of a
    traceback — fail-closed, per §22.
    """


def _require_staging_config(settings: Settings) -> None:
    """Fail closed unless the app is in ``staging`` mode with LinkedIn creds set.

    WHY staging only: this spike publishes a REAL post. Pinning it to
    ``VISION_ENV=staging`` guarantees the post-then-delete safety behaviour, so a
    ``dry_run`` (nothing would post) or ``live`` (post would linger) run is
    refused rather than doing something surprising to the owner's profile.
    """
    if settings.vision_env is not VisionEnv.STAGING:
        raise SpikeConfigError(
            "refusing to run: set VISION_ENV=staging in .env so the test post is "
            f"published then immediately deleted (current: {settings.vision_env.value})."
        )
    # Credentials are required to even build the authorize URL / exchange a code.
    if not settings.li_client_id or not settings.li_client_secret:
        raise SpikeConfigError(
            "missing LinkedIn credentials: set LI_CLIENT_ID and LI_CLIENT_SECRET "
            "in .env (see .env.example and the ONE-TIME SETUP notes in this file)."
        )


def _extract_code(redirected_url: str, expected_state: str) -> str:
    """Parse the OAuth callback URL and return its authorization ``code``.

    Verifies the ``state`` echoed back matches the anti-CSRF nonce we generated
    (§15.1) — a mismatch means a forged/replayed callback and is rejected. The
    returned code is a short-lived secret and is deliberately NOT logged.
    """
    query = parse_qs(urlparse(redirected_url.strip()).query)

    # LinkedIn signals a declined/failed consent with ``error`` instead of code.
    if "error" in query:
        # ``error_description`` is a non-secret human message; surface it as-is.
        detail = query.get("error_description", ["<no description>"])[0]
        raise SpikeConfigError(f"LinkedIn returned an OAuth error: {detail}")

    returned_state = query.get("state", [""])[0]
    if not returned_state or not secrets.compare_digest(returned_state, expected_state):
        raise SpikeConfigError(
            "state mismatch on the OAuth callback — possible CSRF/replay; aborting."
        )

    codes = query.get("code")
    if not codes or not codes[0]:
        raise SpikeConfigError(
            "no authorization 'code' found in the pasted URL — paste the FULL "
            "redirected URL including the ?code=...&state=... query string."
        )
    return codes[0]


def _run_oauth_and_publish(client: LinkedInClient) -> None:
    """Drive the full authorize -> exchange -> publish -> delete round-trip.

    Split out from ``main`` so the flow reads top-to-bottom. Every LinkedIn call
    goes through the shared ``LinkedInClient`` (the exact code the real worker
    uses), and every error surfaces as a typed :class:`LinkedInError` the caller
    handles — no bare excepts, no silent failures (§22).
    """
    # (1) A fresh, high-entropy anti-CSRF state nonce for this authorization.
    state = secrets.token_urlsafe(24)
    authorize_url = client.build_authorize_url(state)

    # The authorize URL is NOT secret (only the public client id + scopes); log
    # it at INFO so the operator can open it. The access token later is not.
    _log.info("open this URL in your browser and grant access:\n%s", authorize_url)

    # (2) Collect the redirected callback URL from the operator. ``input`` is the
    # only interactive touchpoint; the code inside is treated as a secret.
    redirected_url = input(
        "\nafter consenting, paste the FULL redirected URL here and press Enter:\n> "
    )
    code = _extract_code(redirected_url, state)

    # (3) Exchange the code for tokens. The token JSON stays in a local var; only
    # the (non-secret) member URN is logged.
    token_json = client.exchange_code(code)
    access_token = token_json.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise SpikeConfigError("token exchange succeeded but returned no access_token.")

    # (4) Resolve the author identity (OpenID ``sub`` -> urn:li:person:...).
    member_urn = client.get_member_urn(access_token)
    _log.info("resolved member URN: %s", member_urn)

    # (5) Publish the marked test post, then (6) delete it — the staging E2E path.
    post_urn = client.publish_text(access_token, member_urn, _TEST_POST_TEXT)
    _log.info("published test post: %s", post_urn)

    client.delete(access_token, post_urn)
    _log.info("deleted test post %s — staging round-trip complete.", post_urn)


def main() -> int:
    """Entry point: validate config, run the round-trip, return an exit code.

    Returns ``0`` on a clean post-then-delete, ``1`` on any handled failure
    (mis-config, OAuth error, or a LinkedIn API error) so a wrapper can detect
    success without the spike ever crashing with a raw traceback.
    """
    configure_logging()
    settings = get_settings()

    try:
        _require_staging_config(settings)
    except SpikeConfigError as exc:
        _log.error("cannot start spike: %s", exc)
        return 1

    # The client owns one httpx connection pool; the context manager guarantees
    # it is closed even if the flow raises partway through.
    try:
        with LinkedInClient(settings) as client:
            _run_oauth_and_publish(client)
    except SpikeConfigError as exc:
        # Operator/callback problems (bad paste, state mismatch, declined consent).
        _log.error("spike aborted: %s", exc)
        return 1
    except LinkedInError as exc:
        # A LinkedIn API failure (auth/scope/rate/5xx) — the message carries the
        # class + status, never a token or response body.
        _log.error(
            "LinkedIn API error during spike: %s (HTTP %s)",
            exc.__class__.__name__,
            getattr(exc, "status_code", "n/a"),
        )
        return 1

    _log.info("spike_linkedin: SUCCESS — OAuth + publish + delete all worked.")
    return 0


if __name__ == "__main__":
    # Non-zero exit on failure so a manual runner sees the outcome at a glance.
    raise SystemExit(main())

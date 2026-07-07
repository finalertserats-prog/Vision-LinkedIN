"""One-time LinkedIn OAuth authorize + PERSIST tokens (BRD §15.1).

Unlike ``spikes/spike_linkedin.py`` (a throwaway probe that discards its tokens),
this stores the encrypted access + refresh tokens in the database so the daily
pipeline (``vision-publisher``) can publish on your behalf. Run it ONCE; re-run
only if a re-authorisation is ever required (the tokens last ~1 year).

Usage:
    .venv\\Scripts\\python scripts\\authorize_linkedin.py

It prints a consent URL; open it, click Allow, then paste the FULL redirected
``http://localhost:8000/...?code=...&state=...`` URL back at the prompt.
"""
from __future__ import annotations

import logging
import secrets
import sys
from urllib.parse import parse_qs, urlparse

from vision.db.session import create_all, get_session
from vision.publish.oauth import handle_callback, start_authorize

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
_log = logging.getLogger("authorize_linkedin")


def _extract(redirected_url: str) -> tuple[str, str]:
    """Pull the one-time ``code`` and CSRF ``state`` out of the pasted callback URL."""
    query = parse_qs(urlparse(redirected_url.strip()).query)
    code = (query.get("code") or [""])[0]
    state = (query.get("state") or [""])[0]
    if not code or not state:
        raise SystemExit(
            "could not find ?code=...&state=... in the pasted URL — paste the FULL "
            "redirected localhost URL, including the query string."
        )
    return code, state


def main() -> int:
    # Ensure the schema exists (creates vision.db on first run for local/dev).
    create_all()

    # Fresh anti-CSRF state; compared against the value LinkedIn echoes back.
    expected_state = secrets.token_urlsafe(24)
    url = start_authorize(expected_state)

    print("\n" + "=" * 70)
    print("Open this URL in your browser and click Allow:\n")
    print(url)
    print("=" * 70)

    redirected = input(
        "\nAfter consenting, paste the FULL redirected URL here and press Enter:\n> "
    )
    code, returned_state = _extract(redirected)

    # get_session() commits on success -> the encrypted tokens are persisted.
    with get_session() as session:
        member_urn = handle_callback(
            session,
            code=code,
            state=returned_state,
            expected_state=expected_state,
        )

    print("\n" + "=" * 70)
    print(f"SUCCESS — encrypted tokens stored for {member_urn}.")
    print("The daily pipeline (vision-publisher) can now publish on your behalf.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())

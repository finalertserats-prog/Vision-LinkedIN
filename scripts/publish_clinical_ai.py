"""One-shot: publish the clinical-AI architecture post + diagram to LinkedIn.

Owner-approved in-chat. Creates a single ``approved`` news draft (clean text, no
attribution footer — sig mode is off) with the Mermaid diagram attached, then
drives the REAL publisher on THAT draft object only (never a fuzzy "latest"
query, never poll_and_publish which could sweep other due drafts). Prints only
the resulting post URN + URL; no token or secret is ever emitted.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from vision.config import get_settings
from vision.db.models import Draft
from vision.db.session import get_session
from vision.publish.worker import LinkedInPublisher

_POST = Path("prep/clinical_ai_post.md")
_IMAGE = Path("prep/clinical_ai_arch.png").resolve()


def main() -> int:
    settings = get_settings()
    text = _POST.read_text(encoding="utf-8").strip()

    if not settings.image_enabled:
        print("WARN: image lane disabled — would post text-only. Aborting so the "
              "diagram is not silently dropped.")
        return 2
    if not _IMAGE.exists():
        print(f"ERROR: diagram not found at {_IMAGE}")
        return 2

    now = datetime.now(timezone.utc)
    publisher = LinkedInPublisher(settings)
    try:
        with get_session() as session:
            # Neutralize any prior unpublished draft for THIS diagram (e.g. a run
            # aborted at the de-naming gate) so the 5-min publisher cron cannot
            # keep retrying a stale row. Explicit by image_path; local DB only.
            stale = (
                session.query(Draft)
                .filter(
                    Draft.image_path == str(_IMAGE),
                    Draft.post_urn.is_(None),
                    Draft.state != "published",
                )
                .all()
            )
            for row in stale:
                row.state = "rejected"
            if stale:
                print(f"neutralized {len(stale)} stale draft(s) -> rejected")

            draft = Draft(
                content_mode="news",          # clean body only (no council block)
                post_text=text,               # body + hashtags already inline
                state="approved",             # publisher CAS: approved -> queued -> published
                scheduled_for=now - timedelta(minutes=1),  # due
                image_type="concept-illustration",
                image_path=str(_IMAGE),
                image_source="deterministic",  # Mermaid render, not a model
                image_prompt=None,
            )
            session.add(draft)
            session.flush()  # assign id before we hand the object to the worker
            draft_id = str(draft.id)
            print(f"draft {draft_id} created (state={draft.state})")

            result = publisher.publish(session, draft)
            print(f"post-state: {result.state}")
            if result.post_urn:
                print(f"POST_URN: {result.post_urn}")
                print(f"POST_URL: {result.post_url}")
                return 0
            print("NOT PUBLISHED — draft did not reach a live URN. "
                  "Check reauth/failure alerts (state above).")
            return 1
    finally:
        publisher.close()


if __name__ == "__main__":
    raise SystemExit(main())

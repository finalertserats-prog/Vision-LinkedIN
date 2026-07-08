"""Delete the 'council of minds' live post and repost the SAME text with NO image.

LinkedIn has no edit API for media, so removing an image = delete + recreate.
Text-only repost of the identical (em-dash-safe) body; repoints the draft.
"""

from __future__ import annotations

from sqlalchemy import desc, select

from vision.config import get_settings
from vision.council.compose import _strip_em_dashes
from vision.db.models import Draft
from vision.db.session import SessionLocal
from vision.publish.worker import LinkedInPublisher


def main() -> int:
    settings = get_settings()
    worker = LinkedInPublisher(settings)
    session = SessionLocal()
    try:
        draft = session.execute(
            select(Draft).where(Draft.post_urn.isnot(None)).order_by(desc(Draft.created_at))
        ).scalars().first()
        # Target the specific council-of-minds post if it is the latest with a URN.
        if draft is None or "council of minds" not in (draft.post_text or "").lower():
            draft = session.execute(
                select(Draft).where(Draft.post_text.ilike("%council of minds%"))
            ).scalars().first()
        if draft is None:
            print("Could not find the council-of-minds post.")
            return 1

        old_urn = draft.post_urn
        text = _strip_em_dashes(draft.post_text or "")
        print(f"draft   : {draft.id}")
        print(f"old urn : {old_urn}")

        _, token, member = worker._load_credentials(session)
        if old_urn:
            worker._client.delete(token, old_urn)
            print(f"deleted : {old_urn}")
        new_urn = worker._client.publish_text(token, member, text)

        draft.post_urn = new_urn
        draft.image_type = "none"
        draft.image_path = None
        draft.image_urn = None
        session.commit()
        print(f"reposted (text-only): {new_urn}")
        print(f"url: https://www.linkedin.com/feed/update/{new_urn}")
        return 0
    finally:
        session.close()
        worker.close()


if __name__ == "__main__":
    raise SystemExit(main())

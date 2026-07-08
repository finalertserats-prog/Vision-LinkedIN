"""One-off maintenance: replace the live LinkedIn post with an em-dash-free copy.

Owner rule (2026-07-08): em-dashes are an AI tell. The post published yesterday
still contains them. LinkedIn has no edit API (BRD §6), so the fix is
delete + recreate: delete the live post, publish the identical text with
em/en dashes mapped to plain hyphens, and repoint the draft's stored URN.

Text-only (the live post carried no image). Idempotent-ish: if the stored URN
is already gone on LinkedIn, delete() treats 404 as success.
"""

from __future__ import annotations

from sqlalchemy import select

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
            select(Draft).where(Draft.state == "published")
        ).scalars().first()
        if draft is None:
            print("No published draft on record; nothing to fix.")
            return 1

        old_urn = draft.post_urn
        old_text = draft.post_text or ""
        new_text = _strip_em_dashes(old_text)
        em_before = old_text.count("—") + old_text.count("–")
        em_after = new_text.count("—") + new_text.count("–")
        print(f"draft id      : {draft.id}")
        print(f"old post urn  : {old_urn}")
        print(f"em-dashes     : {em_before} -> {em_after}")
        if em_before == 0:
            print("Live post already has no em/en dashes; nothing to do.")
            return 0

        _, access_token, member_urn = worker._load_credentials(session)

        # 1) Delete the live post (404 is treated as already-gone / success).
        if old_urn:
            worker._client.delete(access_token, old_urn)
            print(f"deleted       : {old_urn}")

        # 2) Republish the hyphenated text (no image on the original).
        new_urn = worker._client.publish_text(access_token, member_urn, new_text)
        print(f"republished   : {new_urn}")

        # 3) Repoint the stored draft so DB reflects reality.
        draft.post_urn = new_urn
        draft.post_text = new_text
        session.commit()
        print("db updated     : post_urn + post_text repointed, state stays published")
        return 0
    finally:
        session.close()
        worker.close()


if __name__ == "__main__":
    raise SystemExit(main())

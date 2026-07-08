"""Attach the freshly-generated anime image to the newest pending draft and
re-send its approval email (with the image inline). Recovers a run where agy
timed out and the draft went text-only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import desc, select

from vision.cli.council import _build_council_signed_links, _cutoff_ttl_seconds
from vision.config import get_settings
from vision.db.models import Draft
from vision.db.session import SessionLocal
from vision.mailer.composer import compose_approval_email, inline_image_for
from vision.mailer.sender import get_sender


def main() -> int:
    settings = get_settings()
    session = SessionLocal()
    try:
        draft = session.execute(
            select(Draft).where(Draft.state == "pending_approval").order_by(desc(Draft.created_at))
        ).scalars().first()
        if draft is None:
            print("No pending_approval draft.")
            return 1

        anime = Path("prep") / f"anime_{str(draft.id)[:8]}.png"
        if not anime.is_file():
            print(f"Anime image not found at {anime}; regenerate first.")
            return 1

        # Stamp the draft with the anime illustration.
        draft.image_type = "concept_illustration"
        draft.image_path = str(anime.resolve())
        draft.image_source = settings.image_model
        draft.image_prompt = "anime concept illustration (regenerated)"

        # Re-issue the signed links (single-use tokens) and persist the new approve
        # hash + expiry so the emailed link verifies against this draft.
        now = datetime.now(timezone.utc)
        ttl = _cutoff_ttl_seconds(now, settings)
        links, approve_hash, expires_at = _build_council_signed_links(str(draft.id), settings, ttl)
        draft.approve_token_hash = approve_hash
        draft.token_expires_at = expires_at
        session.commit()

        subject, text, html = compose_approval_email(draft, [], links, settings=settings, now=now)
        part = inline_image_for(draft.image_path, draft.image_type)
        inline = [(part.cid, part.data, part.subtype)] if part is not None else None

        sent = get_sender(settings).send(subject, text, html, inline_images=inline)
        print(f"image_type : {draft.image_type}")
        print(f"image_path : {draft.image_path}")
        print(f"email sent : {sent}")
        print(f"has inline : {inline is not None}")
        return 0 if sent else 1
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())

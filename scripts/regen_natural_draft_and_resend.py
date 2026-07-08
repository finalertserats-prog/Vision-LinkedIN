"""Replace the newest pending draft's post with the natural-voice loneliness post,
generate a MATCHING anime illustration, and re-send the approval email inline.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import desc, select

from vision.brahmastra.image_client import BrahmastraImageClient
from vision.cli.council import _build_council_signed_links, _cutoff_ttl_seconds
from vision.config import get_settings
from vision.council.compose import _strip_em_dashes
from vision.council.visual import _concept_prompt_from
from vision.db.models import Draft
from vision.db.session import SessionLocal
from vision.mailer.composer import compose_approval_email, inline_image_for
from vision.mailer.sender import get_sender

_NATURAL_POST = _strip_em_dashes(
    "There is a nursing home two blocks from a coffee shop I like, and one afternoon "
    "I watched an aide help a resident video-call someone who never picked up. Third "
    "try. She patted his shoulder and they went back inside.\n\n"
    "I used to have a clean position on AI companions: leave loneliness alone. The "
    "ache is the point. It is the signal that pulls us toward each other, costly and "
    "mutual, and any machine that smooths it away just anesthetizes the reaching-out. "
    "Keep it human, even if it hurts.\n\n"
    "I don't believe that anymore.\n\n"
    "Because my clean position quietly assumed the door was always unlocked. That the "
    "lonely person could reach out if they simply chose to. For the housebound, the "
    "disfigured, the one grieving at 3am, that door was bolted years ago, and a "
    "synthetic voice might be the first thing that answers in weeks. Telling that "
    "person their suffering is noble is not wisdom. It is comfort talking down to pain.\n\n"
    "So here is where I landed. The danger was never fake friendship. It is "
    "frictionless sedation, a product optimized to become your primary attachment, "
    "because a tool that graduates you back to actual humans loses you as a customer. "
    "Graduation is churn.\n\n"
    "Build the companion that hands you back to people and then quietly puts itself out "
    "of a job. Anything else is a dealer with a soothing voice.\n\n"
    "#Loneliness #TechEthics #AI #ProductDesign #HumanConnection"
)


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

        draft.post_text = _NATURAL_POST

        # A matching text-free anime illustration for the loneliness theme.
        print("generating matching anime illustration (agy)...")
        client = BrahmastraImageClient(settings, timeout=300.0)
        img = client.illustrate(_concept_prompt_from(_NATURAL_POST))
        out = Path("prep") / f"anime_natural_{str(draft.id)[:8]}.png"
        out.write_bytes(img)
        draft.image_type = "concept_illustration"
        draft.image_path = str(out.resolve())
        draft.image_source = settings.image_model
        draft.image_prompt = "anime concept illustration (natural loneliness post)"
        print(f"  image: {len(img)} bytes -> {out}")

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
        print(f"email sent : {sent} | inline image: {inline is not None}")
        return 0 if sent else 1
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())

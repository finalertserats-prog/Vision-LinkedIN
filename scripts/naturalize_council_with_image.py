"""Repost the council-of-minds post (bd979c3d) with NATURALIZED text + a matching
anime illustration. Targets the draft by explicit UUID (verified by text).
"""

from __future__ import annotations

import uuid
from pathlib import Path

from vision.brahmastra.image_client import BrahmastraImageClient
from vision.config import get_settings
from vision.council.compose import _strip_em_dashes
from vision.council.visual import _concept_prompt_from
from vision.db.models import Draft
from vision.db.session import SessionLocal
from vision.publish.worker import LinkedInPublisher

_COUNCIL_ID = uuid.UUID("bd979c3d-9fbd-4834-b20c-1f28230bec0b")

_NATURAL = _strip_em_dashes(
    "I keep going back and forth on how to think out loud in public.\n\n"
    "One side of me says clarity is the whole job. The feed is drowning in "
    "unresolved hot takes that farm engagement and hand a builder nothing to "
    "actually use. Do the hard analytical work, resolve the ambiguity, give people "
    "a blueprint. Value is decisions closed, not arguments started.\n\n"
    "The other side says the opposite. The best public thinking stages a tension "
    "that isn't settled yet. People trust you faster when they watch your judgment "
    "form in real time than when you hand them a polished conclusion. Certainty too "
    "early usually means the topic was already dead.\n\n"
    "For a long time I couldn't pick a side. Then I realized I was asking the wrong "
    "question.\n\n"
    "The move isn't resolve-or-leave-open. It is this: do the rigorous work "
    "privately, in your own head, until your conviction is actually earned. Then "
    "plant that flag in public as something people can attack. Not a fog to wander "
    "in. Not a finished monument to bookmark. A wall to throw themselves at.\n\n"
    "Because the bookmark-bait blueprint and the lazy both-sides question fail for "
    "the same reason: neither one costs the author anything.\n\n"
    'So maybe the real test was never "did I resolve it?" or "did I leave it open?" '
    'It is "am I willing to be wrong about this, out loud?"\n\n'
    "Still sitting with that one. What do you think?\n\n"
    "#ThinkingOutLoud #Leadership #BuildInPublic #IntellectualHonesty"
)


def main() -> int:
    settings = get_settings()
    worker = LinkedInPublisher(settings)
    session = SessionLocal()
    try:
        draft = session.get(Draft, _COUNCIL_ID)
        assert draft is not None, "council draft not found"

        print("generating matching anime illustration (agy)...")
        client = BrahmastraImageClient(settings, timeout=300.0)
        img = client.illustrate(_concept_prompt_from(_NATURAL))
        out = Path("prep") / f"anime_council_{str(draft.id)[:8]}.png"
        out.write_bytes(img)
        print(f"  image: {len(img)} bytes -> {out}")

        _, token, member = worker._load_credentials(session)
        worker._client.delete(token, draft.post_urn)
        print(f"deleted old post: {draft.post_urn}")
        image_urn = worker._client.upload_image(token, img, owner_urn=member)
        new_urn = worker._client.publish_with_image(token, member, _NATURAL, image_urn)

        draft.post_text = _NATURAL
        draft.post_urn = new_urn
        draft.image_type = "concept_illustration"
        draft.image_path = str(out.resolve())
        draft.image_urn = image_urn
        session.commit()
        print(f"reposted natural + anime: {new_urn}")
        print(f"url: https://www.linkedin.com/feed/update/{new_urn}")
        return 0
    finally:
        session.close()
        worker.close()


if __name__ == "__main__":
    raise SystemExit(main())

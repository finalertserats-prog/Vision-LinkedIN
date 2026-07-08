"""Precise two-post fix (target by explicit UUID, verified by text):
  1. council-of-minds (bd979c3d): remove the quote-card image -> repost text-only.
  2. we-hand-out-medals (f065d478): RESTORE the concept illustration I wrongly
     stripped -> repost with the image.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from vision.config import get_settings
from vision.council.compose import _strip_em_dashes
from vision.db.models import Draft
from vision.db.session import SessionLocal
from vision.publish.worker import LinkedInPublisher

_COUNCIL_ID = uuid.UUID("bd979c3d-9fbd-4834-b20c-1f28230bec0b")
_MEDALS_ID = uuid.UUID("f065d478-0f9b-4617-aba7-3395b885198d")
_MEDALS_IMG = Path("prep/council_f065d478.png")


def main() -> int:
    settings = get_settings()
    worker = LinkedInPublisher(settings)
    session = SessionLocal()
    try:
        _, token, member = worker._load_credentials(session)

        # 1) Remove image from the council-of-minds post (the actual request).
        council = session.get(Draft, _COUNCIL_ID)
        assert council and "council of minds" in (council.post_text or "").lower(), "wrong council draft"
        worker._client.delete(token, council.post_urn)
        print(f"deleted council quote-card post: {council.post_urn}")
        council.post_urn = worker._client.publish_text(
            token, member, _strip_em_dashes(council.post_text or "")
        )
        council.image_type = "none"
        council.image_path = None
        council.image_urn = None
        print(f"council reposted TEXT-ONLY: {council.post_urn}")

        # 2) Restore the concept illustration on the medals post I wrongly stripped.
        medals = session.get(Draft, _MEDALS_ID)
        assert medals and "hand out medals" in (medals.post_text or "").lower(), "wrong medals draft"
        img = _MEDALS_IMG.read_bytes()
        worker._client.delete(token, medals.post_urn)
        print(f"deleted medals text-only post: {medals.post_urn}")
        image_urn = worker._client.upload_image(token, img, owner_urn=member)
        medals.post_urn = worker._client.publish_with_image(
            token, member, _strip_em_dashes(medals.post_text or ""), image_urn
        )
        medals.image_type = "concept_illustration"
        medals.image_path = str(_MEDALS_IMG.resolve())
        medals.image_urn = image_urn
        print(f"medals reposted WITH image restored: {medals.post_urn}")

        session.commit()
        print(f"council url: https://www.linkedin.com/feed/update/{council.post_urn}")
        print(f"medals url : https://www.linkedin.com/feed/update/{medals.post_urn}")
        return 0
    finally:
        session.close()
        worker.close()


if __name__ == "__main__":
    raise SystemExit(main())

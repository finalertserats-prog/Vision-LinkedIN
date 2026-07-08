"""Recovery: publish the newest council draft cleanly, WITH an image, for real.

Context (2026-07-08): the approval web server ran with a NoopPublisher, so
clicking "Post now" marked the draft published but never hit LinkedIn (no URN).
The stored post also opened with a leaked "Here is the post." preamble and had
no image. This one-off honours the owner's publish intent properly:

  1. Clean the stored post text (strip preamble + em-dashes).
  2. Generate a text-free concept illustration via agy (no API key).
  3. Upload the image + publish the post WITH it to LinkedIn.
  4. Repoint the draft (post_urn, image fields) so the DB matches reality.

Falls back to a text-only publish only if image generation fails, and says so.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import desc, select

from vision.brahmastra.image_client import BrahmastraImageClient
from vision.config import get_settings
from vision.council.compose import _strip_em_dashes, _strip_leading_preamble
from vision.council.visual import _concept_prompt_from
from vision.db.models import Draft
from vision.db.session import SessionLocal
from vision.publish.worker import LinkedInPublisher


def main() -> int:
    settings = get_settings()
    worker = LinkedInPublisher(settings)
    session = SessionLocal()
    try:
        draft = session.execute(
            select(Draft).order_by(desc(Draft.created_at)).limit(1)
        ).scalars().first()
        if draft is None:
            print("No draft found.")
            return 1
        if draft.post_urn:
            print(f"Draft already has a live URN ({draft.post_urn}); nothing to do.")
            return 0

        clean = _strip_em_dashes(_strip_leading_preamble(draft.post_text or ""))
        print(f"draft id   : {draft.id}")
        print(f"clean chars: {len(clean)} (was {len(draft.post_text or '')})")
        print(f"opens with : {clean[:48]!r}")

        # 1) Generate the concept illustration (agy, no API key).
        image_bytes: bytes | None = None
        try:
            client = BrahmastraImageClient(settings)
            image_bytes = client.illustrate(_concept_prompt_from(clean))
            img_path = Path(settings.council_image_state_path).expanduser().parent
            img_path.mkdir(parents=True, exist_ok=True)
            out = img_path / f"council_{str(draft.id)[:8]}.png"
            out.write_bytes(image_bytes)
            draft.image_type = "concept_illustration"
            draft.image_path = str(out)
            print(f"image      : generated {len(image_bytes)} bytes -> {out}")
        except Exception as exc:  # noqa: BLE001 - any image failure degrades to text
            print(f"image      : generation FAILED ({exc.__class__.__name__}: {exc})")
            print("             -> falling back to a text-only publish.")

        # 2) Credentials + real publish.
        _, access_token, member_urn = worker._load_credentials(session)
        if image_bytes is not None:
            image_urn = worker._client.upload_image(
                access_token, image_bytes, owner_urn=member_urn
            )
            draft.image_urn = image_urn
            post_urn = worker._client.publish_with_image(
                access_token, member_urn, clean, image_urn
            )
            print(f"published  : WITH image -> {post_urn}")
        else:
            post_urn = worker._client.publish_text(access_token, member_urn, clean)
            print(f"published  : text-only -> {post_urn}")

        # 3) Repoint the draft to reality.
        draft.post_text = clean
        draft.post_urn = post_urn
        draft.state = "published"
        session.commit()
        print(f"live url   : https://www.linkedin.com/feed/update/{post_urn}")
        return 0
    finally:
        session.close()
        worker.close()


if __name__ == "__main__":
    raise SystemExit(main())

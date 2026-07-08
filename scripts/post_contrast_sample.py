"""One-off: publish the anime contrast card with a foundations-first caption."""

from __future__ import annotations

from pathlib import Path

from vision.config import get_settings
from vision.council.compose import _strip_em_dashes
from vision.publish.worker import LinkedInPublisher

_CAPTION = _strip_em_dashes(
    "Everyone is racing to build with AI. Fast demos, fancy features, a shiny "
    "house on stilts.\n\n"
    "Then it wobbles, and no one can quite say why.\n\n"
    "The teams that last are not the fastest. They are the ones who dug down to "
    "the rock first: clean data, clear judgment, and a reason to exist that was "
    "true before the model arrived and stays true long after the hype moves on.\n\n"
    "AI is not the foundation. It is the top floor. Build the ground it stands on "
    "first, and build it to hold weight.\n\n"
    "#AI #Leadership #BuildToLast #Foundations"
)


def main() -> int:
    settings = get_settings()
    worker = LinkedInPublisher(settings)
    img = Path("prep/sample_contrast_card.png").read_bytes()
    try:
        # Reuse the worker's credentials + client; no secrets touch the terminal.
        import vision.db.session as dbs

        session = dbs.SessionLocal()
        try:
            _, token, member = worker._load_credentials(session)
        finally:
            session.close()
        image_urn = worker._client.upload_image(token, img, owner_urn=member)
        post_urn = worker._client.publish_with_image(token, member, _CAPTION, image_urn)
        print(f"published: {post_urn}")
        print(f"url: https://www.linkedin.com/feed/update/{post_urn}")
        return 0
    finally:
        worker.close()


if __name__ == "__main__":
    raise SystemExit(main())

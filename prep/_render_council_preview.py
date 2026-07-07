"""Render the already-committed council draft's approval email + print proof facts.

Fetches the pending_approval council draft produced by the live run, re-renders
its approval email HTML via the mailer composer, writes it to
prep/council_email_preview.html, and prints the proof facts (post head, format,
name-leak check on the PUBLISHED surfaces, 'Powered by Brahmastra' count).
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from vision.approval.tokens import issue_token  # noqa: E402
from vision.cli.council import _COUNCIL_ACTION_PATHS  # noqa: E402
from vision.config import get_settings  # noqa: E402
from vision.council.compose import find_forbidden_name  # noqa: E402
from vision.db.models import Draft  # noqa: E402
from vision.db.session import get_session  # noqa: E402
from vision.mailer.composer import compose_approval_email  # noqa: E402

DRAFT_ID = sys.argv[1]


def main() -> int:
    settings = get_settings()
    now = datetime.now(timezone.utc)

    with get_session() as session:
        draft = session.get(Draft, uuid.UUID(DRAFT_ID))
        if draft is None:
            print(f"[proof] draft {DRAFT_ID} NOT FOUND", flush=True)
            return 1
        meta = draft.council_meta or {}
        post_text = draft.post_text or ""
        council_block = str(meta.get("council_block") or "")
        fmt = str(meta.get("format") or "")
        topic = str(meta.get("topic") or draft.lane_focus or "")

        base = "http://localhost:8000"
        links = {
            action: f"{base}/{path}?token="
            + issue_token(str(draft.id), action, 3600, settings.secret_hmac_key)[0]
            for action, path in _COUNCIL_ACTION_PATHS.items()
        }
        subject, text, html = compose_approval_email(
            draft, [], links, settings=settings, now=now
        )

    preview = REPO / "prep" / "council_email_preview.html"
    preview.write_text(html, encoding="utf-8")

    forbidden = ("Gemini", "Codex", "Claude", "GPT")
    leak_post = find_forbidden_name(post_text)
    leak_block = find_forbidden_name(council_block)

    def scan(label: str, s: str) -> list[str]:
        hits = [tok for tok in forbidden if tok.lower() in s.lower()]
        print(f"[proof] {label}: forbidden tokens present -> {hits or 'NONE'}", flush=True)
        return hits

    print("=" * 70, flush=True)
    print(f"[proof] draft_id      = {draft.id}", flush=True)
    print(f"[proof] content_mode  = {draft.content_mode}", flush=True)
    print(f"[proof] state         = {draft.state}", flush=True)
    print(f"[proof] topic         = {topic}", flush=True)
    print(f"[proof] format        = {fmt}", flush=True)
    print("=" * 70, flush=True)
    print("[proof] POST_TEXT (first 400 chars):", flush=True)
    print(post_text[:400], flush=True)
    print("=" * 70, flush=True)
    scan("post_text     ", post_text)
    scan("council_block ", council_block)
    scan("PUBLISHED(all)", post_text + "\n" + council_block)
    print(f"[proof] find_forbidden_name(post_text)     = {leak_post!r}", flush=True)
    print(f"[proof] find_forbidden_name(council_block) = {leak_block!r}", flush=True)

    sig = "Powered by Brahmastra"
    print(f"[proof] '{sig}' in post_text     = {post_text.count(sig)}", flush=True)
    print(f"[proof] '{sig}' in council_block = {council_block.count(sig)}", flush=True)
    print(f"[proof] '{sig}' in email HTML     = {html.count(sig)}", flush=True)
    print(f"[proof] preview file = {preview}", flush=True)

    name_leak_passed = leak_post is None and leak_block is None
    print(f"[proof] NAME-LEAK CHECK (published) PASSED = {name_leak_passed}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

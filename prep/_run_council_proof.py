"""LIVE end-to-end proof runner for the council autonomous engine.

Runs the REAL vision-council flow once in dry_run:
  * real 3-AI deliberation + compose (live Gemini/Codex/Claude voices)
  * stores a pending_approval council draft (content_mode='council', council_meta)
  * dry_run => composes but does NOT send the approval email
Then re-renders that draft's approval email HTML via the mailer composer with a
MOCK sender, writes it to prep/council_email_preview.html, and prints the proof
facts (post head, format, name-leak check, signature count, preview path).
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from vision.cli.council import _COUNCIL_ACTION_PATHS, run_council_cli  # noqa: E402
from vision.config import get_settings  # noqa: E402
from vision.council.compose import find_forbidden_name  # noqa: E402
from vision.db.models import Draft  # noqa: E402
from vision.db.session import get_session  # noqa: E402
from vision.mailer.composer import compose_approval_email  # noqa: E402


class _MockSender:
    """A no-network EmailSender: records a send would have happened, sends nothing."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def send(self, subject: str, text: str, html: str, to: str | None = None) -> bool:
        self.calls.append((subject, text, html))
        return True


def main() -> int:
    settings = get_settings()
    mode = settings.vision_env
    now = datetime.now(timezone.utc)
    mock = _MockSender()

    print(f"[proof] VISION_ENV = {mode.value}", flush=True)
    print("[proof] running REAL council flow (live voices — may take minutes)...", flush=True)

    with get_session() as session:
        result = run_council_cli(now, mode, session=session, settings=settings, sender=mock)

        draft = session.get(Draft, uuid.UUID(result.draft_id))
        meta = draft.council_meta or {}
        post_text = draft.post_text or ""
        council_block = str(meta.get("council_block") or "")

        # Re-render the approval email HTML the owner would open (mock sender path;
        # dry_run means the CLI itself sent nothing). Rebuild the same 5 signed
        # links the CLI mints so the composer renders the full council email.
        from vision.approval.tokens import issue_token
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

    # --- Name-leak check across the PUBLISHED surfaces + the rendered email ----
    forbidden = ("Gemini", "Codex", "Claude", "GPT")
    leak_post = find_forbidden_name(post_text)
    leak_block = find_forbidden_name(council_block)

    # The email HTML deliberately contains the raw-debate peek which DOES name the
    # voices (owner-only provenance). The task's leak check targets the PUBLISHED
    # assembly: post_text + council_block. We report both: published surfaces must
    # be clean; we also show which forbidden tokens appear where in the email.
    def scan(label: str, s: str) -> None:
        hits = [tok for tok in forbidden if tok.lower() in s.lower()]
        print(f"[proof] {label}: forbidden tokens present -> {hits or 'NONE'}", flush=True)

    print("=" * 70, flush=True)
    print(f"[proof] draft_id      = {result.draft_id}", flush=True)
    print(f"[proof] content_mode  = {result.content_mode}", flush=True)
    print(f"[proof] state         = {draft.state}", flush=True)
    print(f"[proof] email_sent    = {result.email_sent}  (dry_run => must be False)", flush=True)
    print(f"[proof] mock.send calls = {len(mock.calls)}  (dry_run => must be 0)", flush=True)
    print(f"[proof] topic         = {result.topic}", flush=True)
    print(f"[proof] format        = {result.format}", flush=True)
    print("=" * 70, flush=True)
    print("[proof] POST_TEXT (first 400 chars):", flush=True)
    print(post_text[:400], flush=True)
    print("=" * 70, flush=True)
    scan("post_text     ", post_text)
    scan("council_block ", council_block)
    published = post_text + "\n" + council_block
    scan("PUBLISHED(all)", published)
    print(f"[proof] find_forbidden_name(post_text)     = {leak_post!r}", flush=True)
    print(f"[proof] find_forbidden_name(council_block) = {leak_block!r}", flush=True)

    sig = "Powered by Brahmastra"
    print(f"[proof] '{sig}' count in post_text     = {post_text.count(sig)}", flush=True)
    print(f"[proof] '{sig}' count in council_block = {council_block.count(sig)}", flush=True)
    print(f"[proof] '{sig}' count in email HTML     = {html.count(sig)}", flush=True)
    print(f"[proof] preview file = {preview}", flush=True)

    name_leak_passed = leak_post is None and leak_block is None
    print(f"[proof] NAME-LEAK CHECK (published) PASSED = {name_leak_passed}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

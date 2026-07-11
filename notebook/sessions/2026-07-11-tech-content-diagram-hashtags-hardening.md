# Session: 2026-07-11 — Tech content + in-sync diagram lane + hashtag fallback (production hardening)

## What Was Done
- **Tightened compose rules** (`src/vision/council/compose.py` `_build_prompt`): two HARD RULES — "TECH IS THE SPINE" (concrete mechanism; when AI is involved say WHAT changes and HOW) and "LAND ON A LEVER" (every post arrives at a mechanism/trade-off/move, never a pretty resignation). Fixes the "posts are great philosophy but no tech / how-to-overcome" complaint. Verified live.
- **Amplifying image prompt** (`src/vision/council/visual.py`): concept illustration now depicts the post's core idea as a visual metaphor, not generic mood art. Still anime, text-free.
- **New diagram lane (in-sync tech visual):**
  - `src/vision/visuals/diagram_renderer.py` — `render_mermaid` shells out to `mmdc` (mermaid CLI), fail-closed with `DiagramRenderError`. ALL I/O inside one try (a full/read-only temp volume degrades, never crashes the post).
  - `src/vision/council/diagram.py` — `DiagramWriter.diagram_for(post_text)` generates a mermaid diagram FROM the finished post (single-purpose voice prompt), de-name-gated + fail-soft. This is the RELIABLE path; the inline `DIAGRAM:` compose section is unreliable (see decisions).
  - Wired in `engine.py` after compose when `COUNCIL_DIAGRAM_ENABLED` and no inline diagram.
  - `visual.py` gained `IMAGE_TYPE_DIAGRAM` decide/generate/attach/stamp; diagram bypasses the decorative rotation but respects the weekly cap.
- **De-naming fix** (`compose.py` `find_forbidden_name`): removed the "the model" ban (and a later narration-regex attempt). Only AI vendor/brand names remain fail-closed. "the model" is a normal technical noun a tech post needs; the old ban hard-aborted good posts with NO retry.
- **Markdown strip** (`compose.py` `_strip_md_bold`): removes `**bold**` LinkedIn shows as literal asterisks, WITHOUT corrupting tech syntax (`__init__`, `O(n ** 2)`, `2**3**4`, `dict(**a,**b)` all intact — word-boundary + non-space guards; `__` handling dropped).
- **Hashtag fallback** (`src/vision/council/hashtags.py`): `HashtagWriter.hashtags_for(post_text)` generates 3-5 topic-specific hashtags when the composer drops them; de-name-gated, generic-filler-dropped, deduped, capped at 5, fail-soft. Engine appends to `post_text` (publisher renders `post_text` verbatim; the hashtags field is metadata only). `COUNCIL_HASHTAGS_ENABLED` default ON.
- **Published a real post live** (proof the pipeline works): `urn:li:share:7481599944961724416` (RAG abstain-path post) — via driving `LinkedInPublisher.publish` directly on an `approved` draft (the web "Post now" is a no-op locally).
- **Two adversarial code-review passes** (find + verify subagents). Every finding (HIGH: disk-full OSError crash; MEDIUM: narration FP, `_strip_md_bold` tech corruption; LOWs) fixed and re-confirmed.
- **Ops/deploy**: added `mmdc` check to `deploy/preflight.sh` + a note in `deploy/DEPLOY.md` §1; fixed 2 pre-existing ruff errors in `scripts/demo_approval.py` and `scripts/demo_full_run.py`.
- **Fixed a global hook bug** (not this repo): `~/.claude/scripts/hooks/secret-redactor-output.js` false-positive on ordinary code (`token = ...` and code-punctuation values) — added a `looksLikeCode()` guard. Left for the user to commit in their global repo. Also fixed the `UserPromptSubmit` hook timeouts (`invocation-receipts.js`, `mention-router.js`) — an un-`unref()`'d watchdog `setTimeout` kept the process alive past the 2s cap; added `.unref()`.

## Key Decisions Made
- **Diagram from the finished post, NOT inline `DIAGRAM:`.** The headless composing voice (`claude` CLI) routinely returns only the post prose and drops the whole structured output contract (FORMAT/hashtags/COUNCIL/DIAGRAM). The parser salvages the post body via an untagged-prose fallback, so `format=unknown` + no hashtags is EXPECTED, not a parse bug. A decoupled single-purpose prompt over the final text is reliable where the multi-section contract is not.
- **De-naming: drop the "the model" gate entirely.** It's a QUALITY concern (voice), not a security leak (only vendor brands leak the machinery). Making a porous, FP-prone heuristic a fail-closed HARD ABORT killed good tech posts. Vendor tokens stay fail-closed at both compose and publish ends.
- **Diagrams are exempt from the anime/text-free rule** because a diagram is an information graphic rendered DETERMINISTICALLY (precision rule §13.6/D10), like the retired deterministic cards — not diffusion art.
- **Hashtags default ON** (unlike diagram which is default OFF): hashtags fulfil the spec the compose prompt already asks for and use the existing voice transport with no external dependency; the diagram lane shells out to `mmdc` so it stays opt-in.
- **web "Post now" no-op is NOT a code bug** (memory `web-noop-publisher-bug` was stale): `service.post_now` queues the draft (`state=scheduled`) and the `vision-publisher` poller does the real post. The local "did not go through" was because `vision-web`/`vision-publisher` don't run on the dev box. Memory updated.

## What's Pending / Next Steps
- **Deploy to VPS** (owner's manual step): `git pull` / `deploy/deploy.sh`, then (1) install `mmdc` (`npm i -g @mermaid-js/mermaid-cli` + headless-chromium libs), (2) set `COUNCIL_DIAGRAM_ENABLED=true` in the VPS `.env` (hashtags already default-on), (3) ensure `vision-web.service` + `vision-publisher.timer` are running, (4) `VISION_APPROVAL_BASE_URL` reachable.
- **Global hook fix** (`secret-redactor-output.js` `looksLikeCode()` + the two `.unref()` fixes) is in `~/.claude/` working tree — owner said they'll commit it in their other session.
- **Codex CLI review kept failing** (exit 126 on the large diff via its shell wrapper) — used two Claude `code-reviewer` passes instead. Worth fixing the Codex wrapper for large diffs later.
- **Known residual (not fixed, low):** `format=unknown` + empty council block still result from the composer dropping the structured output; only hashtags were backfilled. The council block (email context) and format-variety tracking are still degraded when the composer disobeys. Could add the same decoupled-fallback pattern if it matters.

## Patterns Learned
- **Decoupled "from the finished post" generation** beats a fat multi-section output contract when the model is unreliable at following structure. Same shape reused for diagram + hashtags: single-purpose prompt → validate → de-name → fail-soft. Good template for the council block / format if needed.
- **Fail-closed HARD ABORTS on heuristics are dangerous** — a porous quality check that aborts a whole post is worse than the thing it prevents. Reserve fail-closed for real security invariants (vendor de-naming); handle quality via the prompt.
- **`mmdc` on Windows** resolves as `mmdc.cmd` via `shutil.which`; a `WinError 193` from `subprocess.run` is an `OSError` → caught → degrades. VPS (Linux) is the real target.
- **Real headless-`claude` behavior != the mocked voice in tests** — always verify structured-output features end-to-end with the real voice.

## Files Changed (committed: 0ce7c68, 17390ac, 35849db — all on origin/main)
- `src/vision/config.py` — `council_diagram_enabled`, `diagram_mmdc_cmd`, `diagram_render_timeout_s`, `council_hashtags_enabled`
- `src/vision/council/compose.py` — tech rules, `DiagramSpec`, `_parse_diagram`, DIAGRAM parsing, de-naming fix, `_strip_md_bold`
- `src/vision/council/diagram.py` (new) — `DiagramWriter`
- `src/vision/council/hashtags.py` (new) — `HashtagWriter`
- `src/vision/council/engine.py` — wire DiagramWriter + HashtagWriter after compose
- `src/vision/council/visual.py` — `IMAGE_TYPE_DIAGRAM` lane, amplifying concept prompt
- `src/vision/visuals/diagram_renderer.py` (new) — `render_mermaid`
- `tests/test_council_diagram.py`, `tests/test_council_diagram_writer.py` (new), `tests/test_council_hashtags.py` (new), `tests/test_council.py`
- `deploy/preflight.sh`, `deploy/DEPLOY.md` — `mmdc` dependency
- `scripts/demo_approval.py`, `scripts/demo_full_run.py` — ruff fixes
- Verification: ruff clean, 585 tests pass.

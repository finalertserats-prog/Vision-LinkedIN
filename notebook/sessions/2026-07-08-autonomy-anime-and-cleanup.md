# Session: 2026-07-08 — Autonomy, anime visuals, data cleanup, contrast cards

## What Was Done
- **Em-dash removal (AI-tell fix):** `src/vision/council/compose.py` — prompt rule + deterministic `_strip_em_dashes()` map em/en dashes to hyphens in `_parse_composition`. Republished the already-live post em-dash-free via `scripts/replace_live_post_dedash.py` (urn ...047682 → ...567232).
- **Markdown compose parser:** `_parse_composition` now tolerates the model returning Markdown (`**Format:**`, `---`, backticks) with no literal `POST:` marker — normalizes headers, treats a `---` as the metadata/body divider, salvages untagged prose, guarded by `_MIN_POST_CHARS=200`. Fixed the repeated "empty post body" council crashes.
- **Preamble strip:** `_strip_leading_preamble` drops a leaked "Here is the post." chat preamble (guarded so a genuine "Here is what..." opener survives).
- **Recovery publish:** `scripts/publish_clean_with_image.py` — cleaned + agy-imaged the falsely-"published" draft f065d478 and published for real (urn ...666945).
- **Anime house art style:** `IMAGE_STYLE_GUIDE` default + `.env` set to elevated anime/manga; `src/vision/brahmastra/image_client.py` `_ART_STYLES` rotates sub-styles (anime cel / manga ink / pencil / watercolor / key-visual) per generation.
- **Autonomous scheduling:** `scripts/vision_scheduler.ps1` registers Windows tasks — Council (daily 08:00), Publisher (~3 min poller), Expire (daily 20:00), Retention (weekly Sun 03:30), Web (logon, uvicorn `vision.approval.web:create_app --factory` on :8000). All 5 registered + running.
- **cwd-independent config:** `src/vision/config.py` anchors `.env` to an absolute path (`_ENV_FILE`) so scheduled tasks/services don't silently fall back to dry_run/staging + lose credentials.
- **Data-lifecycle retention:** `src/vision/ops/retention.py` + `src/vision/cli/retention.py` (`vision-retention`) — archive rows/images older than `RETENTION_DAYS` (30) → gzip JSON + `VACUUM INTO` snapshot + images zip → rclone Drive backup with `copy`+`check --one-way` verify → prune + VACUUM. Fail-closed. agy-designed, Codex-reviewed (3 bugs fixed).
- **Anime contrast card:** `render_contrast_card` in `src/vision/visuals/card_renderer.py` (two text-free agy panels + crisp Pillow labels, 1080×1080). Wired into the council image lane (`compose.ContrastSpec` + optional `CONTRAST:` line → `visual.decide_council_image`/`attach_council_image` → `IMAGE_TYPE_CONTRAST`). Posted a live sample (urn ...937088).

## Key Decisions Made
- **Em-dashes are banned** in output (owner's wife flagged them as an AI tell) — hyphens instead, enforced in code not just prompt.
- **All visuals are hand-drawn anime/manga art** (owner is an anime buff), tuned to the editorial end for LinkedIn; panels rotate sub-styles **freely** (owner chose variety over per-post matching).
- **Outbox for publishing:** web marks drafts `scheduled`; the `vision-publisher` poller does the REAL publish. The web's NoopPublisher is harmless once the poller runs. False "published" earlier came from a STALE staging web server squatting on :8000 — kill port 8000 before starting the scheduled Web task.
- **Retention is fail-closed:** never prune without a verified off-box backup; never touch `own_posts`/`oauth_tokens`/in-flight drafts. Backup target = **rclone → Google Drive** (OAuth, no API key). Retention = **30 days**.
- **Contrast card de-naming:** labels/scenes are rendered into the published image, so they go through the forbidden-AI-name gate; a leak DROPS the card (post still ships) rather than failing compose.

## What's Pending / Next Steps
- **Owner one-time step:** `rclone config` → remote `gdrive` OAuth to vishnu.wildeagle@gmail.com, then set `RCLONE_REMOTE=gdrive` in `.env` to activate Drive backups (until then retention archives locally + skips prune).
- **Videos / voice-over / music:** architecture only (`docs/VIDEO_ARCHITECTURE.md`) — Phase 5 build pending; must follow the same anime aesthetic.
- **Tasks run only when logged in** (no stored password). Switch to run-when-logged-off if owner wants.
- Optional: second contrast-card sample on a non-tech metaphor to confirm style holds across topics.

## Council / Team Sessions
- agy (Gemini) designed the retention mechanism and the contrast-card layout spec (background `beast.sh --gemini research`).
- Codex reviewed both retention and the contrast integration; caught 3 retention bugs + 1 publish-safety bug (contrast labels bypassing the name gate) — all fixed.

## Patterns Learned
- **cwd-relative `.env` is a trap** for scheduled tasks/services — anchor config paths absolutely.
- **A stale long-running process can squat a port** and mask a restart (env=staging persisted); check process StartTime + free the port.
- **Anything model-generated that ends up in a PUBLISHED surface (incl. text baked into images) must pass the de-naming gate.**
- **agy image gen** (no API key) is reliable for text-free anime art; keep text deterministic (Pillow) — diffusion mangles words.

## Files Changed (key)
- `src/vision/council/compose.py`, `visual.py`, `engine.py`
- `src/vision/brahmastra/image_client.py`, `src/vision/config.py`
- `src/vision/ops/retention.py`, `src/vision/cli/retention.py`
- `src/vision/visuals/card_renderer.py`
- `scripts/vision_scheduler.ps1`, `replace_live_post_dedash.py`, `publish_clean_with_image.py`, `sample_contrast_card.py`, `post_contrast_sample.py`
- `tests/test_council.py`, `test_council_visual.py`, `test_visuals.py`, `test_retention.py`, `test_brahmastra_client.py`, `pyproject.toml`
- 14 commits (846b1f5 → 4eec35d); 508 tests pass; ruff clean.
```

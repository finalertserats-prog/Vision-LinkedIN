# Session: 2026-08-04 — VPS deployment, --topic flag, content variety, two silent publish bugs

## What Was Done

**`--topic` flag (`src/vision/cli/council.py`)**
- `vision-council` took no arguments; the only way to steer a subject was the FIFO
  queue file. Added `_parse_args()` + `main(argv=None)` (argv injectable so cron's
  bare call is unchanged) threading `--topic` into `run_council_cli` → `run_council`.
- No engine change needed: `engine.py:132` already lets an explicit topic outrank
  both the problem inbox and the topic queue, consuming neither.
- Tests: `tests/test_council_cli.py` (+6).

**Deployed to the VPS (187.127.139.234, shared with production finalert.tech)**
- `/opt/vision`, user `vision`, Python 3.11 (deadsnakes; distro had only 3.10).
- Brahmastra CLI creds + `~/.claude/council` scripts + `.antigravity` copied from
  `/root` to `/home/vision`, plus the agy bootstrap marker
  `~/.claude/state/agy-bootstrapped.json` (agy_call.sh refuses to run without it).
- nginx vhost (name-scoped, never `default_server`) + Let's Encrypt via **snap**
  certbot → https://187-127-139-234.sslip.io
- Timers armed: council 08:00 IST, publisher every 5 min, expire 20:00, retention
  Sun 03:30. Windows scheduled tasks DISABLED to stop double-posting.
- Installed `/usr/local/bin/vision-topic` wrapper: `vision-topic "subject"` runs now,
  `vision-topic --queue "subject"` queues for the next 08:00 run. It sources
  `/opt/vision/.env` because a bare manual run does NOT get `AGY_BIN` etc.

**Content variety (`council/visual.py`, `diagram.py`, `topics.py`, `engine.py`, `config.py`)**
- Owner: posts felt stale — a process-flow diagram on nearly every post, same look
  when art ran, subjects circling the same ground.
- Art is now the default across three registers (anime key art / manga ink /
  painterly), never repeating the previous post's register.
- Diagrams gated by a cooldown, `COUNCIL_DIAGRAM_MIN_GAP` (default 4) ≈ 1 in 5.
- Healthcare down to one domain slot; lighter registers weighted up.
- Recent topics persisted (`COUNCIL_TOPIC_STATE_PATH`) and repetition judged on
  shared theme words. The engine had never passed `recent_topics` at all.
- Tests: `tests/test_council_variety.py` (14).

**`format: "unknown"` fixed (`council/formats.py`, `compose.py`)**
- EVERY council draft since July recorded `format="unknown"`: the composing voice
  reliably drops the `FORMAT:` header. Since `"unknown"` is not a FORMATS key it was
  never remembered, so the variety window stayed empty and format rotation was dead.
- The prompt now ASSIGNS a shape; the recorded format resolves to the echoed value
  when known, else the assigned one — true by construction.
- Tests: `tests/test_council_format_resolution.py` (10).

**Two silent publishing bugs (`publish/linkedin.py`, `config.py`, `.env.example`)**
- **HTTP 426**: `LI_VERSION=202506` had been retired by LinkedIn. Probing `/rest/me`
  showed 202601–202607 accepted, 202608 not yet released. Bumped to 202607.
- **Silent truncation**: a published post was cut mid-sentence at its first `|`.
  `commentary` is parsed as Little Text Format; `\ | { } @ [ ] ( ) < > * _ ~` are
  reserved. Added `_escape_commentary()` at the single payload choke point.
- Tests: `tests/test_linkedin_commentary_escaping.py` (11).

**Deploy-kit bug fixed (`deploy/systemd/*.service`, `deploy/DEPLOY.md`)**
- `vision-web`/`publisher`/`expire` ran `ProtectSystem=strict` with only
  `/opt/vision/logs` writable, so SQLite could not create `-wal`/`-journal` files.
  Every approval, publish and expiry write failed "readonly database" — the
  approval loop could never have worked on this kit. Granted the app dir.

**End-to-end proven on the VPS**: generate → email → click → DB write → publish →
LinkedIn. Live post: `urn:li:share:7490368283024543744`.

## Key Decisions Made

- **`/opt/vision`, not `/opt/VisionLinkedIN`** — the 7 systemd units, `deploy.sh` and
  `preflight.sh` hardcode `/opt/vision`; renaming meant editing all of them for no gain.
- **Copied `vision.db` to the VPS rather than re-authorizing LinkedIn** — the OAuth
  token lives in the `oauth_tokens` table (decryptable with the same `TOKEN_ENC_KEY`),
  AND `alembic upgrade head` cannot build a fresh DB (a revision autoloads
  `oauth_tokens` before it exists). Copying solved schema + auth together.
- **Diagram cooldown, not "not twice in a row"** — the first rule still allowed
  alternating, i.e. half the feed. The very first run under it produced a diagram
  anyway, which is what proved the point. A gap of 4 gives ~1 in 5.
- **Only UNCONDITIONAL formats are assigned** (`provocation`, `uncomfortable_middle`,
  `what_they_missed`, `quiet_observation`). `rare_consensus`, `one_changed_mind`,
  `show_the_split`, `steelman_both` each assert something about what actually happened
  in the deliberation; assigning one up front would manufacture a framing. They stay
  reachable only as a voice-reported override.
- **`#` is NOT escaped for LinkedIn** — live posts prove a bare `#` linkifies, and
  escaping it would force guessing which `#` starts a tag (`C#`, `docs#section-3`).
  Codex's adversarial review killed an earlier regex-based hashtag-detection plan.
- **Reconciliation accepts raw OR escaped commentary** — it matched on exact text, so
  escaping would have broken duplicate detection and risked a double-post.

## What's Pending / Next Steps

- **Verify `_`/`*` rendering.** The app's scopes cannot `GET /rest/posts` (403), so
  escaping could not be machine-verified. If a future post shows a stray backslash,
  drop `_` and `*` from `_LTF_RESERVED` in `publish/linkedin.py`.
- **`LI_VERSION` will expire again** (~annually). Symptom: "publish failed / HTTP 426".
- **Trim the God Mode harness from `/home/vision/.claude`** — every `claude -p` boots
  SessionStart hooks (`bg-orchestrator.js`, `godmode-mesh-health.sh`). Works, but adds
  latency and noise to every council run.
- **`situation` is still empty** on drafts (same dropped-header cause as `format`).
  Provenance only, so lower value, but the same assign-and-resolve trick would fix it.
- **Local `.env` line 90** is a corrupted fragment (Windows path where `\a`/`\b` became
  control chars). Ignored by both parsers; removed on the VPS copy only.
- **Secret-redactor hook false-positives** on `src/vision/cli/council.py` (matches the
  identifier `secret_hmac_key`), blocking edits. Worth a skip-list entry.

## Patterns Learned

- **A 201 is not proof of success.** LinkedIn accepted a truncated post and returned
  201; the confirmation email said "live". Nothing in our logs was wrong. Verify the
  artifact, not the status code — and when the API cannot be read back, say so.
- **"Content-judged" is not a throttle.** The diagram gate always existed (the writer
  could reply NONE) and still fired on nearly every post. Any lane competing for a
  slot needs a cooldown or rotation budget, not just a judgment call.
- **A value that is never recorded cannot be avoided.** `format="unknown"` looked like
  cosmetic provenance; it had silently disabled the entire variety engine for a month.
- **Windows → Linux `.env` needs LF.** Pydantic tolerates CRLF; systemd's
  `EnvironmentFile` does not, and every value arrives with a trailing `\r`.
- **`PrivateTmp=true` isolates `/tmp` per unit** — a PNG the council writes to `/tmp`
  is invisible to the publisher that must attach it. Shared dirs for handoff artifacts.
- **Check a shared host before touching nginx.** This VPS serves production
  finalert.tech; every change was additive and name-scoped. Its cert had also been
  EXPIRED for five weeks (broken system certbot) — renewed via snap certbot.

## Files Changed

- `src/vision/cli/council.py` — `--topic` flag, argv parsing
- `src/vision/config.py` — topic-state path, recent-topic window, diagram min gap, `LI_VERSION`
- `src/vision/council/visual.py` — art registers, style rotation, diagram cooldown, ledger keys
- `src/vision/council/diagram.py` — stricter default-to-NONE prompt
- `src/vision/council/engine.py` — cooldown skip, recent-topic wiring
- `src/vision/council/topics.py` — domain reweighting, `RecentTopicStore`, theme overlap
- `src/vision/council/formats.py` — `UNCONDITIONAL_FORMATS`, `choose_assigned_format`
- `src/vision/council/compose.py` — assigned-shape prompt, format resolution
- `src/vision/publish/linkedin.py` — `_escape_commentary`, `_LTF_RESERVED`, reconciliation
- `deploy/systemd/vision-{web,publisher,expire}.service` — `ReadWritePaths=/opt/vision`
- `deploy/DEPLOY.md` — CRLF, shared image dir, LinkedIn-auth-in-DB traps
- `.env.example` — `LI_VERSION=202607` + warning
- `tests/test_council_cli.py`, `tests/test_council.py` (updated),
  `tests/test_council_variety.py`, `tests/test_council_format_resolution.py`,
  `tests/test_linkedin_commentary_escaping.py`

Commits: `04c1320`, `cb21f57`, `3f9b4c1`, `674978e`, `97ae915`, `812b713`. 626 tests green.

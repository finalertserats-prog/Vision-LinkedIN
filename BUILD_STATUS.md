# Project VISION — Build Status

**Status: build complete. All 5 phases green. 397 tests passing, ruff clean, security-reviewed.**
Everything below is proven against **mocks** — the code is ready; going *live* needs your credentials (see "Wire reality").

---

## What's built (all committed + pushed)

| Phase | Delivers | Tests |
|---|---|---|
| **0** Foundation | Config, DB models (SQLite-dev / Postgres-prod), `BrahmastraClient` (CLI mode), `LinkedInClient`, HMAC signed tokens | ✅ |
| **1** Content pipeline | RSS/API ingest (timeout-bounded, parallel, browser-UA) → dedup → score/select → Brahmastra generate→critique→verify with **server-side grounding gate** → deterministic navy/gold card renderer → own-post 90-day dedup | ✅ |
| **2** Approval loop | Themed email (navy/gold) + FastAPI signed-link endpoints (GET-never-mutates, atomic single-use nonce) + edit page + draft state machine + expiry | ✅ |
| **3** LinkedIn publish | AES-256-GCM token encryption + OAuth + refresh; idempotent publisher (lease + reconcile, no double-post) + image upload + signature modes; DRY_RUN/STAGING/LIVE | ✅ |
| **4** Ops & hardening | Health/canary, durable-dedup alerting, feed-health, daily orchestration (cron lock + idempotency), Docker/systemd/CI/backup, failure-injection suite, docs | ✅ |

**Total: 397 tests passing. Ruff clean.** Every phase was adversarially reviewed by Codex — ~33 high-severity bugs were found and fixed test-first (token-replay, double-post windows, grounding-gate bypass, cron overlap, crash-loops, and the OAuth crypto save/load mismatch).

## Team note
- **Claude** built + fixed. **Codex** reviewed every phase and caught the serious bugs. **Gemini/agy** was unusable in this environment (its Antigravity CLI renders to a TUI that can't be captured headlessly — runs but returns no readable text). Gemini's intended lanes (news ingest, email theming) were covered by reusing your **finalert** patterns.

---

## ⚠️ Wire reality (do this when you're back — nothing here is done yet)

1. **LinkedIn dev app** → create at developer.linkedin.com, link a Company Page (placeholder ok), verify the app, enable "Sign In with LinkedIn (OpenID Connect)" + "Share on LinkedIn". Put `LI_CLIENT_ID` / `LI_CLIENT_SECRET` in `.env`.
2. **Gmail App Password** (or a Resend key) → into `.env` for the approval email.
3. **Generate secrets**: `SECRET_HMAC_KEY`, `TOKEN_ENC_KEY` (random 32+ bytes each).
4. **Run the auth spike**: `spikes/spike_linkedin.py` — one-time OAuth + a STAGING "hello world" post that auto-deletes, proving the whole loop against the live API.
5. **First real run**: `vision-daily` (dry_run → staging → live) → approval email → your Approve click → live post.

See `docs/DEPLOY.md`, `docs/RUNBOOK.md`, `docs/OPERATIONS.md` for the full runbook, and `.env.example` for every setting.

## Known follow-ups (non-blocking, not security issues)
- The daily/publisher/token/expire cron jobs are wired; confirm the crontab/systemd timers on the VPS (`deploy/`).
- Feed list in `prep/sources_seed.yaml` was live-checked; a few sources 403 bot-blockers server-side may need revisiting from the VPS IP.

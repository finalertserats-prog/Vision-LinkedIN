# VISION — Operations Guide

How VISION runs day to day: the scheduled timeline, the three run modes, and how
to promote a run safely from a dry rehearsal to a live post (BRD §10.2, §10.3,
§18.1, §20). For recovery from faults, see [RUNBOOK.md](./RUNBOOK.md).

---

## 1. Processes

VISION is four small, single-purpose processes plus the always-on web tier. Each
is config-driven (`$VISION/.env`, BRD Appendix A) and runs under systemd with
memory limits and restart policy adapted from the finalert deploy patterns —
**mind the prior VPS memory-overload incident**: every unit sets `MemoryMax`,
`OOMScoreAdjust`, and `Restart=always` (see §5).

| Process | Trigger | Role | Console script |
|---|---|---|---|
| `vision-web` | always-on | FastAPI approval service: `/approve`, `/reject`, `/edit`, `/healthz`, OAuth callback. The **only** externally reachable surface — kept tiny, validated, rate-limited, monitored. | `vision.approval.web:create_app` (uvicorn) |
| `vision-daily` | cron ~06:30 IST | Ingest → Curate → Synthesise → Quality → Email; writes one `draft`. | `vision-daily` |
| `vision-publisher` | cron every ~5 min | Publishes `approved && due` drafts; reconciles stranded ones; reaps stuck ones. At-most-once. | `vision-publisher` |
| `vision-token` | cron daily ~02:00 | Refreshes LinkedIn tokens in the refresh window; alerts if re-auth needed. | `vision-token` |
| `vision-expire` | cron at cutoff ~20:00 IST | Auto-expires un-actioned drafts so nothing is posted that day. | `vision-expire` |

---

## 2. Daily timeline (IST; all times configurable via `.env`)

| Time (IST) | Process | What happens |
|---|---|---|
| **06:30** | `vision-daily` | Ingest both lanes (hc + ai) → curate/dedup/score → synthesise (generate → critique → verify → image) → quality gates → store one `draft` in `pending_approval`. |
| **06:45** | (within daily) | Signed approval email sent to the owner (approve / edit / reject / post-now links, HMAC-signed, single-use, short TTL). |
| morning | Human | Owner proof-reads, optionally edits, clicks **Approve** (or **Post now**). The GET shows a confirmation page; only the POST changes state. |
| **09:00** | `vision-publisher` | Publishes the approved-and-due draft (`PUBLISH_SLOT_LOCAL`, default 09:00), captures the post URN/URL, emails a confirmation. |
| **20:00** | `vision-expire` | Any still-`pending_approval` draft is moved to `expired` (`APPROVE_CUTOFF_LOCAL`, default 20:00) — no post today. |
| **02:00** | `vision-token` + backup | Nightly `pg_dump` of the `vision` schema (retain 14 days); token-refresh check. |

Key scheduling guardrails:
- **No overlapping daily runs** — the design assumes exactly one run per day.
- **The publisher is idempotent** — running it more often only reconciles faster;
  a draft with a stored `post_urn` is a strict no-op, so it can never double-post.
- **Expiry is fail-closed** — a parsing/timezone error expires nothing rather than
  killing a valid draft at the wrong instant.

---

## 3. Run modes (`VISION_ENV`) — FR-20

`VISION_ENV` selects how much of the pipeline's side effects are real. It is the
single most important operational switch. Default is the safest.

| Mode | Email | LinkedIn post | Use it for |
|---|---|---|---|
| **`dry_run`** *(default)* | none | **none** — logs only, no state change | Local dev, cron wiring, prompt/quality tuning. Proves the whole pipeline with zero external effect. |
| **`staging`** | to the owner | **posts a clearly-marked test post, then immediately deletes it** | End-to-end validation against the *live* LinkedIn API without leaving anything on the profile (LinkedIn has no draft state, so post-then-delete is the honest E2E — BRD §18.1). |
| **`live`** | to the owner | **real publish** + confirmation email | Production. |

How each mode behaves in the publisher (all enforced in `vision.publish.worker`):
- `dry_run` returns before any credential load or network call — nothing posts,
  no state advances.
- `staging` runs the *entire* real path (claim → publish → capture URN) then calls
  `delete` on the test post and reverts the draft to its schedulable slot, so the
  loop is proven but the profile stays clean.
- `live` finalises `queued → published` atomically with the URN and sends exactly
  one confirmation.

`PUBLISH_MODE` is an orthogonal switch: `api` (official `/rest/posts`, default) vs
`prefill` (degraded manual-composer fallback for when the API is unavailable).

---

## 4. How to promote a run (dry_run → staging → live)

Promotion is a deliberate, checklist-gated ladder. **Never** jump straight to
`live` on a new deploy or after a config change.

### Step A — Rehearse in `dry_run`
1. Set `VISION_ENV=dry_run` in `.env`.
2. Run the pipeline and read the logs (no post, no email):
   ```bash
   $VISION/.venv/bin/python -m vision.cli.daily
   ```
3. Confirm: a `draft` row is created, quality gates pass (char length, hook, ≤ N
   hashtags, grounding % ≥ `GROUNDING_MIN_PCT`, dedup, no banned phrases, no
   unresolved template tokens), and `model_trace` shows the intended lanes.

### Step B — Validate the full loop in `staging`
1. Ensure LinkedIn is authorised (one-time; see RUNBOOK §1) and `/healthz` is `ok`.
2. Set `VISION_ENV=staging`. Run daily, receive the approval email, click Approve.
3. Let `vision-publisher` run. Confirm in the logs it **posted then deleted** the
   marked test post, and that nothing remains on the owner's profile.
4. Confirm the confirmation path fired and the draft returned to its schedulable
   state (staging never leaves a live post).

### Step C — Go `live`
1. Only after A and B are green. Set `VISION_ENV=live`.
2. Verify the pre-flight checklist (below), then restart the affected services:
   ```bash
   systemctl restart vision-web
   # daily/publisher/token/expire are cron/timer-driven; they pick up .env next tick
   ```
3. Watch the first real cycle end-to-end: approval email → approve → `published`
   with a real `post_url` → confirmation email. Verify the post on LinkedIn.

### Pre-flight checklist before `live`
- [ ] `/healthz` returns `ok` with `token_secret: configured` (not the dev default).
- [ ] LinkedIn tokens present and not near expiry (`vision-token` reports OK).
- [ ] `SECRET_HMAC_KEY` and `TOKEN_ENC_KEY` are strong, non-default, and stored in
      the secret manager — not beside the DB, never in the image.
- [ ] Email delivery confirmed (a staging approval email actually arrived).
- [ ] A recent backup exists and a restore has been tested (RUNBOOK §6).
- [ ] Feeds healthy (`sources.last_ok_at` recent; no stale-feed alert open).
- [ ] Memory limits + restart policy active on all units (§5).

### Rolling back a promotion
If anything looks wrong in `live`, set `VISION_ENV=dry_run` (or `staging`) and
restart `vision-web`. In-flight approved drafts are safe: the publisher never
double-posts, and a draft with a stored `post_urn` is a no-op. Investigate with
the RUNBOOK before re-promoting.

---

## 5. Deploy & resource guardrails (finalert-adapted)

Deployment mirrors finalert's proven ops shape, adapted for VISION and **hardened
against the prior VPS memory-overload incident** (BRD §19, §21):

- **`deploy.sh`**: `git pull` → `pip install -r` → `systemctl restart` with an
  `is-active` guard so a service is only restarted if it was already running
  (never accidentally starts a stopped unit).
- **systemd units** (one per process) carry, on every unit:
  - `MemoryMax=<cap>` — a hard ceiling so no VISION process can exhaust the VPS
    (the memory-overload lesson). Keep caps modest; the ingest fan-out is bounded
    to 6 workers precisely to stay within them.
  - `OOMScoreAdjust` — the health/canary path is protected from the OOM killer;
    heavy one-shot jobs are the preferred victims, never the always-on web tier.
  - `Restart=always` + `WatchdogSec=300` — auto-recover from a crash; the watchdog
    fires only where the process actually implements `sd_notify` (avoid dead
    watchdog config that gives false safety).
- **Secrets** injected at runtime from the secret manager — never baked into an
  image or committed. `.env` is not in git.
- **TLS everywhere** via the existing reverse proxy; the approval endpoints are
  the only public surface and are per-IP + per-token rate-limited.

---

## 6. Observability

- **Structured JSON logs** per stage, correlated by `run_id` / `draft_id`, with
  secrets redacted (bearer tokens, OAuth tokens, HMAC tokens, query strings).
- **`/healthz`** reports pipeline + DB + token status; a canary pings it and
  alerts on failure (finalert canary pattern).
- **Alerts** (email) on: daily-run failure/partial, publish failure, dead_letter,
  re-auth needed, dead feed / stale source. Each alert carries only error *class* +
  status code — never a token, body, or draft content.
- **`audit_log`** is append-only: every state change and publish, with actor +
  timestamp + nonce hash. This is the source of truth when reconstructing an
  incident — read it, don't trust memory.

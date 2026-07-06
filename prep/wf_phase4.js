export const meta = {
  name: 'vision-phase4',
  description: 'Project VISION Phase 4 — ops/observability/hardening: health+canary, alerting+feed-health, daily orchestration, deploy (Docker/systemd/CI), failure-injection, final security review',
  phases: [
    { title: 'Construct', detail: '5 parallel agents: observability, alerting+feed-health, daily orchestration, deploy, failure-injection+runbook' },
    { title: 'Review', detail: 'per-module Codex + Claude review' },
    { title: 'Verify', detail: 'venv + pytest; build-error-resolver fixes red' },
    { title: 'Harden', detail: 'full-system DRY_RUN + Codex adversarial security review of the whole codebase' },
  ],
}

const ROOT = 'D:\\\\Projects\\\\ClaudeCode\\\\Vision-LinkedIN'
const BRD = `${ROOT}\\\\VISION_BRD_v1.md`
const FINALERT = 'D:\\\\Projects\\\\ClaudeCode\\\\_scratch\\\\finalert'
const THREAT = `${ROOT}\\\\prep\\\\security_threatmodel.md`
const CONV = `ENGINEERING CONVENTIONS (BRD §22 — MANDATORY):
- Fully-commented (WHY); type hints; immutable; no bare except; specific exceptions; logging module (never print, never log secrets); pathlib; f-strings; config over code.
- Reuse EXISTING code across all phases — do NOT redefine: src/vision/config.py, logging_setup.py, db/*, ingest/*, curate/*, synthesise/*, visuals/*, mailer/*, approval/* (web, state_machine, tokens), publish/* (worker, oauth, token_refresh). Wire them together; don't rebuild.
- Tests part of done: pytest AAA; mock all external I/O (network/SMTP/subprocess/LinkedIn). No real posts/emails/model calls in tests.
- REUSE finalert ops patterns (read-only clone ${FINALERT}): deploy/systemd/*.service (OOMScoreAdjust=-700, MemoryMax, Watchdog=300s, Restart=always — mind the prior VPS memory-overload incident), backup.py (pg_dump + retention), deploy.sh (git pull→pip→systemctl restart). Adapt, don't import.
- SECURITY per ${THREAT}: fail-closed everywhere; secrets redacted in logs; least privilege; the approval endpoints are the only external surface — keep tiny + validated + rate-limited + monitored.`

const MANIFEST = { type:'object', additionalProperties:false, required:['files','summary'], properties:{ files:{type:'array',items:{type:'object',additionalProperties:false,required:['path','purpose'],properties:{path:{type:'string'},purpose:{type:'string'}}}}, summary:{type:'string'}, tests_pass:{type:'boolean'}, notes:{type:'string'} } }
const REVIEW = { type:'object', additionalProperties:false, required:['module','verdict','issues'], properties:{ module:{type:'string'}, verdict:{type:'string',enum:['pass','pass_with_nits','needs_fix']}, issues:{type:'array',items:{type:'object',additionalProperties:false,required:['severity','description'],properties:{severity:{type:'string',enum:['high','medium','low']},file:{type:'string'},line:{type:'string'},description:{type:'string'},suggested_fix:{type:'string'}}}}, codex_ran:{type:'boolean'} } }
const VERIFY = { type:'object', additionalProperties:false, required:['passed','summary'], properties:{ passed:{type:'boolean'}, summary:{type:'string'}, failing_tests:{type:'array',items:{type:'string'}} } }

phase('Construct')
const MODULES = [
  { key:'observability', label:'construct:observability', task:
`Build OBSERVABILITY for Project VISION (BRD §17, NFR-08). ${CONV}
Create ONLY these NEW files:
- src/vision/ops/health.py — health_status(session) -> {db, tokens(access/refresh expiry), last_run, feeds_ok} and a FastAPI router GET /healthz (mountable on the existing approval web app) returning 200/503 with the status JSON.
- src/vision/ops/run_record.py — helpers to open/close a runs row (status ok|partial|failed, stats jsonb: counts, timings, token usage, model versions) correlated by run_id; a contextmanager record_run().
- src/vision/ops/canary.py — a canary() that pings /healthz and returns pass/fail (reuse-the-FinalAlert-pattern comment); main() cron entry that alerts on failure.
- tests/test_observability.py — AAA: healthz returns ok when healthy and 503 when DB/token bad (mock); run_record writes/closes a runs row with stats; canary detects a failing health endpoint (mock httpx). No real network.
Register vision-canary entry in pyproject.toml.
Return manifest.` },

  { key:'alerting', label:'construct:alerting-feedhealth', task:
`Build ALERTING + FEED-HEALTH for Project VISION (BRD §17, NFR-07/08). ${CONV}
Create ONLY these NEW files:
- src/vision/ops/alerts.py — an AlertChannel abstraction with an EmailAlertChannel (reuse src/vision/mailer) and an optional TelegramAlertChannel (httpx, config-gated); alert(kind, subject, detail) for kinds: daily_run_failure, publish_failure, token_reauth_needed, dead_feed, dead_letter. Dedup/rate-limit repeated alerts. Never include secrets.
- src/vision/ops/feed_health.py — check_feed_health(session, now): flag sources whose last_ok_at is older than a configurable threshold (or never), emit a dead_feed alert, and optionally auto-disable persistently-dead feeds. Update last_ok_at on successful ingest (helper used by ingest).
- tests/test_alerting.py — AAA: each alert kind routes to the channel (mock), dedup suppresses repeats, telegram gated by config, feed-health flags a stale source + emits alert + optional auto-disable, healthy feed untouched. Mock mailer/httpx. No real network.
Return manifest.` },

  { key:'daily', label:'construct:daily-orchestration', task:
`Build the DAILY ORCHESTRATION for Project VISION — the glue that runs the whole pipeline (BRD §10.2/§10.3, FR-01..09, FR-20 modes). This wires EXISTING modules; do not rebuild them. ${CONV}
Create/UPDATE ONLY these files:
- src/vision/cli/daily.py (REPLACE the Phase-0 stub) — run_daily(now, mode): open a run_record; ingest (FeedFetcher over enabled sources) → normalise → persist items → curate.select_top → synthesise.pipeline (generate→critique→verify + quality_report + image decision/render) → build a draft row (state pending_approval) → own-post dedup check → mailer.compose_approval_email + send (respect FR-20 modes: dry_run=no email/no post, staging=email to self, live=real) → close run_record. Robust: one failing stage degrades gracefully (partial run) with an alert, never crash-loops. main() cron entry.
- src/vision/cli/publisher.py + src/vision/cli/token.py (UPDATE the Phase-0 stubs to call the real worker.poll_and_publish / token_refresh.refresh_if_needed).
- tests/test_daily_orchestration.py — AAA: full run with EVERYTHING mocked (feeds, BrahmastraClient, mailer, publisher) produces a pending_approval draft + a run row; dry_run sends no email; a failing ingest lane still completes with a partial run + alert; modes respected. No real network/model/email.
Return manifest.` },

  { key:'deploy', label:'construct:deploy', task:
`Build DEPLOYMENT for Project VISION (BRD §19). Reuse finalert patterns at ${FINALERT}\\\\deploy. ${CONV}
Create ONLY these NEW files:
- Dockerfile — slim python:3.11, install the package, non-root user, entrypoint configurable (web/daily/publisher/token/canary).
- docker-compose.yml — services: vision-web (FastAPI, always-on), postgres (pgvector image) for prod, and one-shot job services for daily/publisher/token/canary; env from .env; memory limits (mem_limit) to honour the prior VPS memory incident; healthcheck on vision-web.
- deploy/systemd/vision-web.service, vision-daily.service+.timer, vision-publisher.service+.timer, vision-token.service+.timer, vision-canary.service+.timer — adapt finalert units (OOMScoreAdjust, MemoryMax, Watchdog, Restart=always, StandardOutput→logs).
- deploy/deploy.sh — git pull → pip install -e . → systemctl restart (with is-active safety checks).
- deploy/crontab.example — documented schedule (daily 06:30 IST, publisher every 5 min, token daily 02:00, backup 02:00) per BRD §10.3.
- scripts/backup.py — nightly pg_dump of the vision schema (sqlite copy fallback for dev), retain 14 days, log result. Adapt finalert backup.py.
- .github/workflows/ci.yml — GitHub Actions: install + pytest + ruff on push/PR.
- docs/DEPLOY.md — VPS deploy runbook (one-time setup, secrets injection, TLS via existing reverse proxy).
- tests/test_deploy_smoke.py — AAA: Dockerfile/compose parse (basic lint), backup.py retention logic (mock filesystem), crontab lines well-formed. No real docker/network.
Return manifest.` },

  { key:'resilience', label:'construct:failure-injection', task:
`Build the FAILURE-INJECTION suite + RUNBOOK for Project VISION (BRD §18.3, §17 runbook). ${CONV}
Create ONLY these NEW files:
- tests/test_failure_injection.py — AAA, all mocked: simulate and assert GRACEFUL, no-double-post behaviour for: dead feed (ingest continues), LLM timeout + invalid JSON (synthesis fails loudly, run marked partial, alert), LinkedIn 401 (refresh→reauth alert, draft preserved), 403 (alert), 429 (backoff), 5xx (backoff→dead_letter+alert), expired token click (rejected, no state change), duplicate approve (idempotent no-op), publish retry never double-posts (idempotency key).
- docs/RUNBOOK.md — documented procedures: re-authorise LinkedIn, backfill a missed day, replay a failed publish (dead_letter), disable a bad feed, rotate secrets, restore from backup.
- docs/OPERATIONS.md — the daily timeline, modes (DRY_RUN/STAGING/LIVE), and how to promote a run.
Return manifest.` },
]
const built = (await parallel(MODULES.map(m => () =>
  agent(m.task, { label:m.label, schema:MANIFEST, phase:'Construct' }).then(r => ({ ...m, manifest:r }))
))).filter(Boolean)
log(`Construct: ${built.length}/${MODULES.length} modules built`)

phase('Review')
const reviews = (await parallel(built.map(m => () =>
  agent(
`Review the '${m.key}' module of Project VISION for correctness, BRD compliance, ops-safety, and SECURITY per ${THREAT}. Files: ${(m.manifest?.files||[]).map(f=>f.path).join(', ')}.
1. Read them. 2. Codex second opinion: bash ~/.claude/council/codex_call.sh "Review these VISION ${m.key} files. Focus: crash-loop safety, secrets in logs, fail-closed on errors, memory guardrails, no-double-post under failure, tests mock all I/O. Terse, file:line. Files: ${(m.manifest?.files||[]).map(f=>f.path).join(' ')}" review 2 30  (if it errors set codex_ran=false).
3. Merge. Report only; do not fix.`,
    { label:`review:${m.key}`, schema:REVIEW, phase:'Review' }
  )
))).filter(Boolean)
const high = reviews.flatMap(r => (r.issues||[]).filter(i => i.severity==='high'))
log(`Review: ${reviews.filter(r=>r.verdict!=='needs_fix').length}/${reviews.length} pass; ${high.length} high issues`)

phase('Verify')
let verify = await agent(`Verify Project VISION Phase 4. In ${ROOT}: .venv/Scripts/pip install -e ".[dev]", then .venv/Scripts/python -m pytest -q. Report pass/fail + failing tests. Do not fix.`, { label:'verify-pytest', schema:VERIFY, phase:'Verify' })
let round = 0
while (!verify?.passed && round < 2) {
  round++; log(`Verify red — fix round ${round}`)
  await agent(`Phase 4 tests failing: ${JSON.stringify(verify?.failing_tests||verify?.summary)}. In ${ROOT}: diagnose + FIX root cause, re-run .venv/Scripts/python -m pytest -q until green. ${CONV} Return what changed.`, { label:`fix:round${round}`, phase:'Verify', agentType:'build-error-resolver' })
  verify = await agent(`Re-run in ${ROOT}: .venv/Scripts/python -m pytest -q. Report pass/fail + failing tests.`, { label:`verify:round${round}`, schema:VERIFY, phase:'Verify' })
}

phase('Harden')
// Full-system DRY_RUN + Codex adversarial whole-codebase security review, in parallel.
const [dryrun, secreview] = await parallel([
  () => agent(
`Run a full-system DRY_RUN of Project VISION end-to-end. In ${ROOT}: set VISION_ENV=dry_run and run the daily pipeline entry with EVERYTHING external mocked/stubbed (no real feeds/models/email/LinkedIn) — either via the vision-daily main with injected fakes or a scripts/demo_full_run.py you create. Assert it produces a pending_approval draft + quality_report + (optional) card, sends NO real email, posts NOTHING, and writes a run record. Report pass/fail + what ran.`,
    { label:'dry-run-e2e', schema:VERIFY, phase:'Harden' }
  ),
  () => agent(
`Adversarial SECURITY REVIEW of the whole Project VISION codebase. Read ${THREAT}. Get Codex's take: bash ~/.claude/council/codex_call.sh "Adversarially review Project VISION (src/vision) for security holes: can any post be published without a valid unexpired single-use approval? any token replay/forgery? OAuth tokens ever unencrypted or logged? secrets in logs/CLI args? GET endpoints that mutate? injection in FastAPI? fail-open paths? Give concrete file:line findings, severity-ranked." review 2 40  — then read the flagged files yourself and produce a final ranked findings list (high/medium/low) with file:line and fix. Report findings only; do not fix.`,
    { label:'security-review', schema:REVIEW, phase:'Harden' }
  ),
])
const secHigh = (secreview?.issues||[]).filter(i => i.severity==='high')

return {
  modules_built: built.map(m=>m.key),
  reviews: reviews.map(r=>({module:r.module,verdict:r.verdict,high:(r.issues||[]).filter(i=>i.severity==='high').length,codex_ran:r.codex_ran})),
  construct_high_issues: high,
  unit_tests_passed: !!verify?.passed,
  dry_run_passed: !!dryrun?.passed,
  security_review_high: secHigh,
  security_verdict: secreview?.verdict,
}

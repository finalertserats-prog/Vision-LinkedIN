export const meta = {
  name: 'vision-phase2',
  description: 'Project VISION Phase 2 — approval loop: mailer (finalert-reuse), FastAPI signed-link endpoints, edit page, state machine, expiry; publishing mocked',
  phases: [
    { title: 'Construct', detail: '4 parallel agents: state machine, mailer, approval web service + edit page, expiry job' },
    { title: 'Review', detail: 'per-module Codex + Claude review (threat-model driven)' },
    { title: 'Verify', detail: 'venv + pytest; build-error-resolver fixes red' },
    { title: 'Integrate', detail: 'E2E approval loop: compose email → verify signed link → state transitions → publish mock called exactly once' },
  ],
}

const ROOT = 'D:\\\\Projects\\\\ClaudeCode\\\\Vision-LinkedIN'
const BRD = `${ROOT}\\\\VISION_BRD_v1.md`
const FINALERT = 'D:\\\\Projects\\\\ClaudeCode\\\\_scratch\\\\finalert'
const THREAT = `${ROOT}\\\\prep\\\\security_threatmodel.md`
const CONV = `ENGINEERING CONVENTIONS (BRD §22 — MANDATORY):
- Fully-commented (WHY); type hints; immutable; no bare except; specific exceptions; logging module (never print); pathlib; f-strings.
- Config over code (env via src/vision/config.py Settings). Secrets never in code/logs; redact.
- Reuse EXISTING Phase-0/1 code — do NOT redefine: src/vision/db/models.py (drafts, audit_log, used_tokens, oauth_tokens...), src/vision/approval/tokens.py (issue_token/verify_token — HMAC single-use keyed on decoded nonce, canonical-base64 enforced), src/vision/config.py, src/vision/synthesise (quality_report shape).
- SECURITY driven by ${THREAT} (Codex threat model): GET requests NEVER mutate (show confirmation only), POST performs the state change; verify signature+expiry+single-use BEFORE any state change; single-use nonce consumed ATOMICALLY with the state transition (compare-and-set); security headers (Referrer-Policy: no-referrer, no public docs, restrictive CORS); rate limits per IP+token; generic errors; fail-closed on any ambiguity.
- Publishing is MOCKED in Phase 2 (a PublisherPort interface called exactly once on approve; real LinkedIn wired in Phase 3).
- Tests part of done: pytest AAA, FastAPI TestClient, mock SMTP/HTTP/publish. No real email sent, no real network.
- REUSE finalert email (read-only clone ${FINALERT}): alerts/email_alerts.py (EmailAlerter SMTP STARTTLS/SSL + Gmail App Password + dedup) and alerts/email_theme.py (wrap_shell/conf_bar/bias_chip). ADAPT into src/vision/mailer/ with a provider abstraction; re-palette to navy/gold (BRD §13.6). Do not import from finalert.`

const MANIFEST = { type:'object', additionalProperties:false, required:['files','summary'], properties:{ files:{type:'array',items:{type:'object',additionalProperties:false,required:['path','purpose'],properties:{path:{type:'string'},purpose:{type:'string'}}}}, summary:{type:'string'}, tests_pass:{type:'boolean'}, notes:{type:'string'} } }
const REVIEW = { type:'object', additionalProperties:false, required:['module','verdict','issues'], properties:{ module:{type:'string'}, verdict:{type:'string',enum:['pass','pass_with_nits','needs_fix']}, issues:{type:'array',items:{type:'object',additionalProperties:false,required:['severity','description'],properties:{severity:{type:'string',enum:['high','medium','low']},file:{type:'string'},line:{type:'string'},description:{type:'string'},suggested_fix:{type:'string'}}}}, codex_ran:{type:'boolean'} } }
const VERIFY = { type:'object', additionalProperties:false, required:['passed','summary'], properties:{ passed:{type:'boolean'}, summary:{type:'string'}, failing_tests:{type:'array',items:{type:'string'}} } }

phase('Construct')
const MODULES = [
  { key:'state_machine', label:'construct:state-machine', task:
`Build the DRAFT STATE MACHINE for Project VISION (BRD §10.4). ${CONV}
Create ONLY these NEW files:
- src/vision/approval/state_machine.py — the transition graph new→drafted→pending_approval→approved→queued→published, plus rejected/expired/publish_failed/dead_letter. A DraftState enum; ALLOWED transitions map; transition(session, draft, to_state, actor, meta) that: validates the transition is allowed (else raise IllegalTransition), enforces rules (only pending_approval→approved|rejected; a VALID unexpired token required to reach approved; published is terminal + idempotent — re-approve is a no-op), writes an append-only audit_log row (entity, entity_id, action, actor, ip, meta, at), and updates draft.state atomically. Fully documented.
- src/vision/approval/state_errors.py — IllegalTransition, TokenRequired, etc.
- tests/test_state_machine.py — AAA: legal path, each illegal transition raises, published idempotent no-op, approved requires token, audit_log row written per transition. In-memory SQLite via conftest.
Return manifest.` },

  { key:'mailer', label:'construct:mailer', task:
`Build the MAILER for Project VISION (BRD §14.1, Appendix B; reuse finalert email). ${CONV}
Create ONLY these NEW files:
- src/vision/mailer/theme.py — HTML email theming adapted from finalert alerts/email_theme.py, re-paletted to navy/gold (settings.CARD_BRAND_PALETTE): wrap_shell(title, subtitle, body, kpi), chip(), bar(), button(text, url). Inline CSS (email-safe).
- src/vision/mailer/sender.py — provider abstraction: an EmailSender protocol; SMTPSender (STARTTLS 587 / SSL 465, Gmail App Password, multipart plain+HTML, error-specific handling — adapted from finalert EmailAlerter) and ResendSender (httpx POST to Resend API). Factory get_sender(settings) by settings.EMAIL_PROVIDER (smtp|resend). Secrets from config, never logged.
- src/vision/mailer/dedup.py — suppress duplicate sends within a window (adapt finalert dedup; atomic JSON/db state) so a re-run doesn't double-send the day's approval email.
- src/vision/mailer/composer.py — compose_approval_email(draft, sources, signed_links) → (subject, text, html) per BRD §14.1/Appendix B: subject 'VISION daily draft — {focus} — {date}', the exact post text with char count, quality report (grounding %, dedup, tone/compliance flags, confidence), sources list, and Approve/Post-now/Edit/Reject buttons (signed URLs passed in), footer (run id, expiry). compose_confirmation_email(draft, post_url). Image preview embedded if draft has an image.
- tests/test_mailer.py — AAA: SMTPSender builds correct MIME + calls smtplib (mocked, no real send), ResendSender posts correct payload (respx mock), factory selects by provider, composer renders all sections + buttons + char count + quality report, dedup suppresses a second identical send. NO real email.
Return manifest.` },

  { key:'web', label:'construct:approval-web', task:
`Build the FastAPI APPROVAL SERVICE for Project VISION (BRD §14.2/§14.3, security per ${THREAT}). Use the EXISTING src/vision/approval/tokens.py + state_machine. ${CONV}
Create ONLY these NEW files:
- src/vision/approval/web.py — FastAPI app 'vision-web' with: GET /approve, /reject, /edit, /post-now → verify the signed token (signature+expiry+single-use check WITHOUT consuming) and render a small confirmation HTML page with a POST form (GET NEVER mutates — threat-model rule). POST /approve etc. → re-verify + ATOMICALLY consume the single-use nonce together with the state transition (compare-and-set), then call the injected PublisherPort/enqueue (approve → schedule_for next slot or 'post now'); reject → discard (+ optional single regen flag); edit handled below. GET /healthz → pipeline+DB+token status. Security: Referrer-Policy no-referrer + security headers middleware, docs disabled in prod, restrictive CORS, per-IP+token rate limiting (simple in-memory/redis-optional limiter), generic error pages ('link no longer valid'), fail-closed.
- src/vision/approval/service.py — the port/logic: PublisherPort protocol (publish is MOCKED in Phase 2 — a NoopPublisher records calls; real one in Phase 3), approve/reject/edit_apply functions operating via the state machine, next-slot scheduling from settings.PUBLISH_SLOT_LOCAL.
- src/vision/approval/edit_page.py — minimal HTML edit page (served by web): pre-filled post text + hashtags, live char count (inline JS), 'Approve edited' POST that replaces post_text and re-runs length/format/compliance checks (NOT full LLM) before allowing approve.
- tests/test_approval_web.py — AAA with FastAPI TestClient: GET shows confirmation and does NOT change state; POST approve transitions state + consumes token + calls publisher EXACTLY ONCE; invalid/expired/replayed token → friendly rejection + no state change; reject path; edit updates text + re-validates; /healthz ok; security headers present. Mock the publisher. No real network.
Return manifest.` },

  { key:'expiry', label:'construct:expiry-job', task:
`Build the DRAFT EXPIRY job for Project VISION (BRD FR-16: un-actioned drafts auto-expire after cutoff, default 20:00 IST — no post that day). ${CONV}
Create ONLY these NEW files:
- src/vision/cli/expire.py — expire_stale_drafts(session, now): find pending_approval drafts past settings.APPROVE_CUTOFF_LOCAL for today and transition them to 'expired' via the state machine (audit-logged). A main() entry for cron. Timezone-correct (settings.TZ). Idempotent.
- tests/test_expiry.py — AAA: a past-cutoff pending draft expires; a still-in-window draft does not; an already-approved/published draft untouched; tz handling. In-memory SQLite.
Also register the vision-expire console entry in pyproject.toml.
Return manifest.` },
]
const built = (await parallel(MODULES.map(m => () =>
  agent(m.task, { label:m.label, schema:MANIFEST, phase:'Construct' }).then(r => ({ ...m, manifest:r }))
))).filter(Boolean)
log(`Construct: ${built.length}/${MODULES.length} modules built`)

phase('Review')
const reviews = (await parallel(built.map(m => () =>
  agent(
`Review the '${m.key}' module of Project VISION for correctness, BRD compliance, and SECURITY per ${THREAT}. Files: ${(m.manifest?.files||[]).map(f=>f.path).join(', ')}.
1. Read them. 2. Codex second opinion: bash ~/.claude/council/codex_call.sh "Review these VISION ${m.key} files. Focus: does GET ever mutate state? is single-use nonce consumed atomically with the transition (no replay/double-approve)? is publish called at most once? secrets in logs? tests mock all external I/O? Terse, file:line. Files: ${(m.manifest?.files||[]).map(f=>f.path).join(' ')}" review 2 30  (if it errors set codex_ran=false).
3. Merge. Report only; do not fix.`,
    { label:`review:${m.key}`, schema:REVIEW, phase:'Review' }
  )
))).filter(Boolean)
const high = reviews.flatMap(r => (r.issues||[]).filter(i => i.severity==='high'))
log(`Review: ${reviews.filter(r=>r.verdict!=='needs_fix').length}/${reviews.length} pass; ${high.length} high issues`)

phase('Verify')
let verify = await agent(
`Verify Project VISION Phase 2. In ${ROOT}: .venv/Scripts/pip install -e ".[dev]" (new deps if any), then .venv/Scripts/python -m pytest -q. Report pass/fail + failing tests. Do not fix.`,
  { label:'verify-pytest', schema:VERIFY, phase:'Verify' }
)
let round = 0
while (!verify?.passed && round < 2) {
  round++
  log(`Verify red — fix round ${round}`)
  await agent(`Phase 2 tests failing: ${JSON.stringify(verify?.failing_tests||verify?.summary)}. In ${ROOT}: diagnose + FIX root cause, re-run .venv/Scripts/python -m pytest -q until green. ${CONV} Return what changed.`, { label:`fix:round${round}`, phase:'Verify', agentType:'build-error-resolver' })
  verify = await agent(`Re-run in ${ROOT}: .venv/Scripts/python -m pytest -q. Report pass/fail + failing tests.`, { label:`verify:round${round}`, schema:VERIFY, phase:'Verify' })
}

phase('Integrate')
const integ = await agent(
`Write + run an E2E APPROVAL-LOOP integration test for Project VISION. In ${ROOT}: create tests/test_integration_phase2.py that (1) seeds a pending_approval draft with a quality_report, (2) issues signed approve/reject/edit tokens (real tokens module), (3) composes the approval email (assert subject/post/quality/sources/buttons + expiry footer present) using a MOCK sender (no real send), (4) drives the FastAPI approval service via TestClient: GET /approve shows confirmation and does NOT mutate; POST /approve consumes the token + transitions to approved/queued + calls the mock PublisherPort EXACTLY ONCE; a replay POST of the same token is rejected and does NOT publish again; an expired token is rejected; the edit flow updates text and re-approves. Assert audit_log rows exist for each transition. Run it: .venv/Scripts/python -m pytest tests/test_integration_phase2.py -q. Also write scripts/demo_approval.py that composes a sample approval email to prep/sample_email.html (mock send) and run it. ${CONV}
Return whether it passes + artifacts produced.`,
  { label:'integrate-approval', schema:VERIFY, phase:'Integrate' }
)

return {
  modules_built: built.map(m=>m.key),
  reviews: reviews.map(r=>({module:r.module,verdict:r.verdict,high:(r.issues||[]).filter(i=>i.severity==='high').length,codex_ran:r.codex_ran})),
  high_severity_issues: high,
  unit_tests_passed: !!verify?.passed,
  integration_passed: !!integ?.passed,
  integration_summary: integ?.summary,
}

export const meta = {
  name: 'vision-phase3',
  description: 'Project VISION Phase 3 — real LinkedIn publishing: OAuth+token encryption/refresh, publisher worker (idempotent, retries, error matrix), image upload, signature modes, DRY_RUN/STAGING/LIVE',
  phases: [
    { title: 'Construct', detail: '4 parallel agents: OAuth+crypto, token-refresh job, publisher worker, image finalisation + signature modes' },
    { title: 'Review', detail: 'per-module Codex + Claude review (security + idempotency focus)' },
    { title: 'Verify', detail: 'venv + pytest; build-error-resolver fixes red' },
    { title: 'Integrate', detail: 'E2E publish loop against a MOCK LinkedIn (STAGING/LIVE need real creds — spike scripts provided)' },
  ],
}

const ROOT = 'D:\\\\Projects\\\\ClaudeCode\\\\Vision-LinkedIN'
const BRD = `${ROOT}\\\\VISION_BRD_v1.md`
const THREAT = `${ROOT}\\\\prep\\\\security_threatmodel.md`
const CONV = `ENGINEERING CONVENTIONS (BRD §22 — MANDATORY):
- Fully-commented (WHY); type hints; immutable; no bare except; specific exceptions; logging module (never print, never log tokens); pathlib; f-strings; config over code.
- Reuse EXISTING code — do NOT redefine: src/vision/publish/linkedin.py (LinkedInClient: build_authorize_url/exchange_code/refresh/get_member_urn/upload_image/publish_text/publish_with_image/delete + typed errors NeedsReauth/RateLimited/TransientLinkedInError), src/vision/db/models.py (oauth_tokens with *_enc LargeBinary, drafts with post_urn/post_url/image_urn/state, audit_log), src/vision/approval/state_machine.py, src/vision/mailer (confirmation email), src/vision/brahmastra/image_client.py (BrahmastraImageClient), src/vision/config.py Settings.
- SECURITY per ${THREAT}: OAuth tokens use authenticated envelope encryption (AES-256-GCM or Fernet) with the key from settings.TOKEN_ENC_KEY, separate from ciphertext; never log/CLI-arg tokens; per-account refresh lock; atomic token replacement; ciphertext versioning.
- IDEMPOTENCY: publish at most once — idempotency key = draft_id; if draft.post_urn already set, no-op. Retries with exponential backoff (tenacity), capped, then dead_letter + alert. Error matrix BRD §15.4 (401→refresh→if still 401 alert reauth, keep the approved draft; 403 alert; 429 backoff; 5xx backoff→dead_letter).
- MODES: settings.VISION_ENV dry_run|staging|live. DRY_RUN: no real post. STAGING: publish then immediately delete a marked test post. LIVE: publish for real. Image failure degrades gracefully to text-only (never blocks publish).
- Tests part of done: pytest AAA; MOCK LinkedInClient/HTTP entirely (no real network, no real post). The real STAGING/LIVE run happens via spike scripts only when creds exist.`

const MANIFEST = { type:'object', additionalProperties:false, required:['files','summary'], properties:{ files:{type:'array',items:{type:'object',additionalProperties:false,required:['path','purpose'],properties:{path:{type:'string'},purpose:{type:'string'}}}}, summary:{type:'string'}, tests_pass:{type:'boolean'}, notes:{type:'string'} } }
const REVIEW = { type:'object', additionalProperties:false, required:['module','verdict','issues'], properties:{ module:{type:'string'}, verdict:{type:'string',enum:['pass','pass_with_nits','needs_fix']}, issues:{type:'array',items:{type:'object',additionalProperties:false,required:['severity','description'],properties:{severity:{type:'string',enum:['high','medium','low']},file:{type:'string'},line:{type:'string'},description:{type:'string'},suggested_fix:{type:'string'}}}}, codex_ran:{type:'boolean'} } }
const VERIFY = { type:'object', additionalProperties:false, required:['passed','summary'], properties:{ passed:{type:'boolean'}, summary:{type:'string'}, failing_tests:{type:'array',items:{type:'string'}} } }

phase('Construct')
const MODULES = [
  { key:'oauth_crypto', label:'construct:oauth-crypto', task:
`Build OAuth authorize + token ENCRYPTION for Project VISION (BRD §15.1/§15.3, security per ${THREAT}). ${CONV}
Create ONLY these NEW files:
- src/vision/publish/crypto.py — authenticated envelope encryption for OAuth tokens: encrypt(plaintext, key)->bytes and decrypt(ciphertext, key)->str using AES-256-GCM (cryptography lib) with a random nonce prepended and a version byte for rotation; associated-data = account/member id; key derived from settings.TOKEN_ENC_KEY. Never log plaintext. Typed CryptoError.
- src/vision/publish/oauth.py — LinkedIn 3-legged OAuth glue over the EXISTING LinkedInClient: start_authorize(state) -> url (validate state), handle_callback(session, code, state) -> exchange code, fetch member_urn (userinfo sub), store access+refresh tokens ENCRYPTED in oauth_tokens with expiries, return member_urn. load_tokens(session)/save_tokens(session,...) decrypt/encrypt helpers. Validate OAuth state (CSRF).
- tests/test_crypto_oauth.py — AAA: encrypt→decrypt round-trip, tampered ciphertext fails auth (raises), wrong key fails, oauth callback stores ENCRYPTED tokens (assert stored bytes != plaintext) + member_urn, state mismatch rejected. Mock LinkedInClient HTTP. No real network.
Return manifest.` },

  { key:'token_refresh', label:'construct:token-refresh', task:
`Build the TOKEN REFRESH job for Project VISION (BRD §15.3, FR-17). ${CONV}
Create ONLY these NEW files:
- src/vision/publish/token_refresh.py — refresh_if_needed(session, now): if access token within settings refresh-window (e.g. 7 days) of expiry, use the refresh token via LinkedInClient.refresh, store new encrypted tokens (atomic replacement, per-account lock to avoid refresh races). If refresh token near expiry or refresh fails → emit a reauth-needed alert (via mailer/alerts) and DO NOT lose state. main() cron entry (vision-token). All token values encrypted; never logged.
- tests/test_token_refresh.py — AAA: near-expiry triggers refresh + stores new encrypted tokens; healthy token untouched; refresh failure raises alert (mock) not crash; concurrent-refresh lock respected. Mock LinkedInClient + mailer. No real network.
Register vision-token entry in pyproject.toml.
Return manifest.` },

  { key:'publisher', label:'construct:publisher-worker', task:
`Build the PUBLISHER WORKER for Project VISION (BRD §15.2/§15.4, FR-12/13/14). Replace the Phase-2 mock PublisherPort with the REAL implementation. ${CONV}
Create ONLY these NEW files:
- src/vision/publish/worker.py — LinkedInPublisher implementing the PublisherPort from Phase 2: publish(draft) that (1) idempotency: if draft.post_urn set → no-op return; (2) load+decrypt tokens; (3) if draft has an approved image: upload via LinkedInClient.upload_image → image_urn, publish_with_image; else publish_text; (4) apply POST_SIGNATURE_MODE text_footer if configured; (5) on success: store post_urn+post_url, transition state→published (state machine), send confirmation email; (6) error matrix §15.4 with tenacity backoff (401→token_refresh→retry once→else NeedsReauth alert + keep approved; 403 alert; 429 backoff; 5xx capped backoff→dead_letter + alert). Modes: dry_run (log only), staging (publish then delete marked test post), live (real). poll_and_publish(session, now): find approved & due drafts (scheduled_for<=now), publish each. main() cron entry (vision-publisher).
- tests/test_publisher.py — AAA with a MOCKED LinkedInClient: idempotent no-op when post_urn set; text publish stores urn + transitions + sends confirmation (mock mailer) EXACTLY once; image publish path; 401→refresh→retry; repeated 5xx→dead_letter+alert; dry_run posts nothing; staging deletes after post; due-filtering in poll_and_publish. No real network.
Register vision-publisher entry in pyproject.toml.
Return manifest.` },

  { key:'image_final', label:'construct:image-finalise', task:
`Finalise the IMAGE lane + SIGNATURE modes for Project VISION (BRD §13.6 Step 4, §15.6, D9/D10). ${CONV}
Create ONLY these NEW files:
- src/vision/visuals/style_guide.py — the fixed concept-illustration style guide (settings.IMAGE_STYLE_GUIDE: minimal, professional, muted palette, no text, no logos, editorial) + LinkedIn dimension/format validation (≈1200x627 or 1200x1200; size/format checks before upload) + a per-week image cap check (settings.IMAGE_MAX_PER_WEEK) querying drafts.
- src/vision/visuals/signature.py — apply_signature(post_text, card_bytes, mode) per POST_SIGNATURE_MODE (off | card_watermark [watermark already on cards] | text_footer [append settings.POST_SIGNATURE_TEXT] | both). Pure, config-driven.
- src/vision/publish/image_upload.py — prepare_and_upload(client, access_token, image_bytes): validate dims/format/size (style_guide), then LinkedInClient.upload_image → image_urn; graceful failure returns None so caller degrades to text-only.
- tests/test_image_final.py — AAA: style guide validates good/bad dims, weekly cap enforced, signature modes produce correct text/footer, upload validates before calling client (mock), failure → None (degrade). Mock LinkedInClient + BrahmastraImageClient. No real gen/network.
Return manifest.` },
]
const built = (await parallel(MODULES.map(m => () =>
  agent(m.task, { label:m.label, schema:MANIFEST, phase:'Construct' }).then(r => ({ ...m, manifest:r }))
))).filter(Boolean)
log(`Construct: ${built.length}/${MODULES.length} modules built`)

phase('Review')
const reviews = (await parallel(built.map(m => () =>
  agent(
`Review the '${m.key}' module of Project VISION for correctness, idempotency, and SECURITY per ${THREAT}. Files: ${(m.manifest?.files||[]).map(f=>f.path).join(', ')}.
1. Read them. 2. Codex second opinion: bash ~/.claude/council/codex_call.sh "Review these VISION ${m.key} files. Focus: can a post be published more than once (idempotency)? are OAuth tokens ever logged or left unencrypted? is the 401/refresh/reauth path correct and non-lossy? does image failure block publishing (it must NOT)? are all retries capped? tests mock all network? Terse, file:line. Files: ${(m.manifest?.files||[]).map(f=>f.path).join(' ')}" review 2 30  (if it errors set codex_ran=false).
3. Merge. Report only; do not fix.`,
    { label:`review:${m.key}`, schema:REVIEW, phase:'Review' }
  )
))).filter(Boolean)
const high = reviews.flatMap(r => (r.issues||[]).filter(i => i.severity==='high'))
log(`Review: ${reviews.filter(r=>r.verdict!=='needs_fix').length}/${reviews.length} pass; ${high.length} high issues`)

phase('Verify')
let verify = await agent(`Verify Project VISION Phase 3. In ${ROOT}: .venv/Scripts/pip install -e ".[dev]", then .venv/Scripts/python -m pytest -q. Report pass/fail + failing tests. Do not fix.`, { label:'verify-pytest', schema:VERIFY, phase:'Verify' })
let round = 0
while (!verify?.passed && round < 2) {
  round++; log(`Verify red — fix round ${round}`)
  await agent(`Phase 3 tests failing: ${JSON.stringify(verify?.failing_tests||verify?.summary)}. In ${ROOT}: diagnose + FIX root cause, re-run .venv/Scripts/python -m pytest -q until green. ${CONV} Return what changed.`, { label:`fix:round${round}`, phase:'Verify', agentType:'build-error-resolver' })
  verify = await agent(`Re-run in ${ROOT}: .venv/Scripts/python -m pytest -q. Report pass/fail + failing tests.`, { label:`verify:round${round}`, schema:VERIFY, phase:'Verify' })
}

phase('Integrate')
const integ = await agent(
`Write + run an E2E PUBLISH integration test for Project VISION against a MOCK LinkedIn (no real creds/network). In ${ROOT}: create tests/test_integration_phase3.py that (1) seeds an approved+due draft (with and without an image), (2) stores encrypted OAuth tokens, (3) runs LinkedInPublisher.publish with a MOCKED LinkedInClient asserting: text post stores post_urn+post_url + transitions to published + sends confirmation (mock) EXACTLY once; a second publish of the same draft is a no-op (idempotent); image draft uploads image then publishes_with_image; a 401 triggers refresh then retry; repeated 5xx → dead_letter + alert; image-gen failure degrades to text-only and still publishes; dry_run posts nothing. Run: .venv/Scripts/python -m pytest tests/test_integration_phase3.py -q.
ALSO create the spike scripts (do NOT run — they need real creds): spikes/spike_linkedin.py (authorize URL → callback → publish 'hello world' test post → delete, using STAGING mode) with clear comments on the one-time setup (BRD §15.1) and a README note that it requires LI_CLIENT_ID/SECRET in .env. ${CONV}
Return whether the integration test passes + list the spike scripts created.`,
  { label:'integrate-publish', schema:VERIFY, phase:'Integrate' }
)

return {
  modules_built: built.map(m=>m.key),
  reviews: reviews.map(r=>({module:r.module,verdict:r.verdict,high:(r.issues||[]).filter(i=>i.severity==='high').length,codex_ran:r.codex_ran})),
  high_severity_issues: high,
  unit_tests_passed: !!verify?.passed,
  integration_passed: !!integ?.passed,
  integration_summary: integ?.summary,
  note: 'LIVE/STAGING against real LinkedIn is gated on owner credentials (LI_CLIENT_ID/SECRET + one-time OAuth). spikes/spike_linkedin.py runs it when creds exist.',
}

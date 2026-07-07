export const meta = {
  name: 'vision-phase3-fix',
  description: 'Fix 7 high-severity Codex findings in Phase 3 — OAuth store race, refresh validation, publisher at-most-once (idempotency/lease/reconcile), image graceful-degrade',
  phases: [
    { title: 'Schema', detail: 'OAuth token store: UniqueConstraint(provider,member_urn) + migration + upsert; fix affected fixtures' },
    { title: 'Fix', detail: 'publisher idempotency/lease/reconcile + image degrade; token-refresh validation + per-account isolation' },
    { title: 'Verify', detail: 'consolidated full suite + fix loop' },
  ],
}

const ROOT = 'D:\\\\Projects\\\\ClaudeCode\\\\Vision-LinkedIN'
const THREAT = `${ROOT}\\\\prep\\\\security_threatmodel.md`
const CONV = `CONVENTIONS (BRD §22): fully-commented WHY; type hints; specific exceptions, no bare except; logging (never log tokens); fail-closed §22.9; immutable. STRICT TDD: write the FAILING test first (RED), confirm it fails, then fix (GREEN). Use .venv (.venv/Scripts/python, .venv/Scripts/pip). Reuse existing code; do not redefine models/clients. During iteration run ONLY your module's tests (other agents edit concurrently); the workflow runs the consolidated full suite afterward.`
const VERIFY = { type:'object', additionalProperties:false, required:['passed','summary'], properties:{ passed:{type:'boolean'}, summary:{type:'string'}, failing_tests:{type:'array',items:{type:'string'}} } }
const REPORT = { type:'object', additionalProperties:false, required:['summary'], properties:{ summary:{type:'string'}, files_changed:{type:'array',items:{type:'string'}}, tests_pass:{type:'boolean'} } }

// ── Stage 1 (barrier): schema change to the shared OAuth token store ──
phase('Schema')
const schema = await agent(
`Fix high-severity Codex issue 1 in Project VISION (repo ${ROOT}). ${CONV}
ISSUE — src/vision/publish/oauth.py + src/vision/db/models.py (OAuthToken ~L242-272): token replacement is only atomic in-process (an in-memory threading.Lock keyed by member_urn), the DB commit happens outside the lock, and OAuthToken has NO UniqueConstraint on (provider, member_urn) (member_urn is even nullable). Two cron/worker processes refreshing the same account can both see None and INSERT → later scalar_one_or_none() raises MultipleResultsFound. This is threat-model §3 'atomic token replacement / refresh races'.
FIX: (a) add UniqueConstraint(provider, member_urn) to the OAuthToken model and make member_urn NOT NULL for real rows; (b) add an Alembic migration under src/vision/db/migrations/versions/ for that constraint; (c) in oauth.py save_tokens/handle_callback, use an UPSERT (or SELECT ... FOR UPDATE within the transaction) so concurrent processes serialise and duplicate inserts fail fast; keep the threading.Lock only as an in-process optimisation.
CRITICAL: this schema change can break OTHER tests that instantiate OAuthToken without member_urn — grep the whole tests/ tree for OAuthToken( and fix every instantiation/fixture to include a member_urn so the suite stays green.
RED test first (in tests/test_crypto_oauth.py): inserting two OAuthToken rows with the same (provider, member_urn) raises an IntegrityError; save_tokens twice for the same account UPDATES rather than duplicating.
Run .venv/Scripts/python -m pytest tests/test_crypto_oauth.py -q and report. Return files changed + tests_pass.`,
  { label:'fix:oauth-store', schema:REPORT, phase:'Schema' }
)
log(`Schema fix: ${schema?.tests_pass ? 'green' : 'see report'}`)

// ── Stage 2 (parallel): publisher path + token refresh (disjoint files) ──
phase('Fix')
const [pub, refresh] = await parallel([
  () => agent(
`Fix 4 high-severity Codex issues (the at-most-once / no-double-post guarantees) in Project VISION (repo ${ROOT}). ${CONV} You OWN these files: src/vision/publish/worker.py, src/vision/publish/linkedin.py, src/vision/publish/image_upload.py. Read ${THREAT}.

ISSUE 4 (worker.py ~340-374) — publish-then-persist crash window: the post is created BEFORE post_urn is committed. A crash between leaves a stranded 'queued' draft (approved post silently never publishes, owner never alerted) or, on re-drive, a double-post.
ISSUE 5 (worker.py ~663-666) — _claim() treats an existing 'queued' as a successful claim and re-publishes; no lease owner/expiry → a second caller double-posts.
ISSUE 6 (worker.py ~591-609 + linkedin.py) — _retrying_post() retries the create on RateLimited/Transient; a 5xx/timeout AFTER LinkedIn created the post duplicates within a single run (Posts API is non-idempotent, no idempotency key sent).
ISSUE 7 (image_upload.py ~72-78 + linkedin.py upload_image) — only LinkedInError is caught; raw httpx.TimeoutException/TransportError from upload_image propagate and BLOCK publishing (violates BRD §13.6: image failure must NEVER block the text post).

COHERENT FIX (not point-patches):
- Add a durable per-draft idempotency key: generate + persist it on the draft BEFORE the create call (a 'publish_attempted'/idempotency marker).
- Do NOT blind-retry the create call on an UNKNOWN outcome (5xx/timeout after send): treat unknown-outcome create as needing RECONCILIATION, not blind retry. On re-drive of a 'queued' draft, RECONCILE — look up whether a post already exists for that idempotency key (or via a stored provider reference) instead of blindly re-posting.
- _claim(): attach a lease (owner id + expiry); do NOT publish an existing 'queued' unless the lease expired AND reconciliation confirms no post exists.
- Add a reaper/alert for drafts stuck in 'queued' beyond a lease TTL (so an approved draft is never silently lost).
- Persist post_urn durably right after a confirmed create; reconcile on restart.
- ISSUE 7: wrap LinkedInClient.upload_image's httpx .post/.put in try/except mapping httpx.TimeoutException/TransportError → TransientLinkedInError, AND broaden image_upload.prepare_and_upload's except to also catch (httpx.HTTPError, OSError) → return None (degrade to text-only, never raise).
RED tests first (tests/test_publisher.py, tests/test_image_final.py): (a) create returning unknown-outcome (Transient after send) does NOT produce two posts in one run; (b) re-driving a 'queued' draft reconciles instead of double-posting; (c) an expired lease is required before re-claim; (d) a draft stuck in 'queued' past TTL triggers an alert; (e) upload_image raising raw httpx.TimeoutException → prepare_and_upload returns None and the text post still publishes.
Run .venv/Scripts/python -m pytest tests/test_publisher.py tests/test_image_final.py -q. Return files changed + tests_pass.`,
    { label:'fix:publisher', schema:REPORT, phase:'Fix' }
  ),
  () => agent(
`Fix 2 high-severity Codex issues in Project VISION token refresh (repo ${ROOT}). ${CONV} You OWN src/vision/publish/token_refresh.py (+ its test).
ISSUE 2 (~450-465) — the LinkedIn refresh response is consumed with ZERO schema validation: payload['access_token'] KeyErrors if absent and silently encrypts the literal 'None' if null; int(expires_in)/int(refresh_token_expires_in) raise ValueError on non-numeric. A malformed/hostile refresh JSON corrupts the stored credential or throws mid-mutation.
FIX: validate the payload with a Pydantic model (access_token: non-empty str REQUIRED; expires_in / refresh_token_expires_in: optional non-negative int) BEFORE mutating the token row. On validation failure, route to a reauth/dead-letter RefreshOutcome — do NOT raise, do NOT half-write.
ISSUE 3 (~691-707) — the per-account loop in refresh_if_needed has no try/except around _refresh_one; any unexpected exception aborts refresh for ALL remaining accounts (contract promises 'one bad account cannot abort the whole run').
FIX: wrap the per-account _refresh_one in try/except that logs (account_id only, never token) and emits a failed/dead-letter RefreshOutcome, so the loop always continues.
RED tests first (tests/test_token_refresh.py): (a) a refresh payload missing access_token (or with null / non-numeric expiry) does NOT corrupt the stored token and yields a reauth/failed outcome (no raise); (b) one account raising an unexpected error still lets the other accounts refresh.
Run .venv/Scripts/python -m pytest tests/test_token_refresh.py -q. Return files changed + tests_pass.`,
    { label:'fix:token-refresh', schema:REPORT, phase:'Fix' }
  ),
])
log(`Fixes: publisher=${pub?.tests_pass?'green':'?'} refresh=${refresh?.tests_pass?'green':'?'}`)

// ── Stage 3: consolidated full suite + fix loop ──
phase('Verify')
let verify = await agent(`Run the CONSOLIDATED full suite for Project VISION. In ${ROOT}: .venv/Scripts/python -m pytest -q and .venv/Scripts/ruff check src. Report pass/fail + any failing tests (cross-module interactions from the parallel fixes, e.g. OAuthToken fixtures needing member_urn). Do not fix, just report.`, { label:'verify-consolidated', schema:VERIFY, phase:'Verify' })
let round = 0
while (!verify?.passed && round < 3) {
  round++; log(`Consolidated red — fix round ${round}`)
  await agent(`Full suite failing after the Phase-3 fixes: ${JSON.stringify(verify?.failing_tests||verify?.summary)}. In ${ROOT}: diagnose + FIX root cause (likely OAuthToken fixtures missing member_urn after the new NOT NULL/UniqueConstraint, or a mock signature drift). Re-run .venv/Scripts/python -m pytest -q until green. ${CONV} Return what changed.`, { label:`fix:round${round}`, phase:'Verify', agentType:'build-error-resolver' })
  verify = await agent(`Re-run in ${ROOT}: .venv/Scripts/python -m pytest -q + ruff check src. Report pass/fail + failing tests.`, { label:`verify:round${round}`, schema:VERIFY, phase:'Verify' })
}

return {
  schema_fix: schema?.summary,
  publisher_fix: pub?.summary,
  refresh_fix: refresh?.summary,
  all_tests_passed: !!verify?.passed,
  verify_summary: verify?.summary,
}

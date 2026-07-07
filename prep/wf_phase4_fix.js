export const meta = {
  name: 'vision-phase4-fix',
  description: 'Fix all high-severity Codex findings from Phase 4 — fail-closed ordering, cron-overlap lock, crash-loop guards, durable alert dedup, deploy hardening + final security sweep',
  phases: [
    { title: 'Fix', detail: '4 parallel agents (daily, cli-guards, alerts, deploy) — each re-runs Codex on its files and fixes all highs via TDD' },
    { title: 'Verify', detail: 'consolidated full suite + fix loop' },
    { title: 'Security', detail: 'Codex whole-codebase security re-review of fixed code; fix any remaining highs; re-verify' },
  ],
}

const ROOT = 'D:\\\\Projects\\\\ClaudeCode\\\\Vision-LinkedIN'
const THREAT = `${ROOT}\\\\prep\\\\security_threatmodel.md`
const CONV = `CONVENTIONS (BRD §22): fully-commented WHY; type hints; specific exceptions, no bare except; logging (never log tokens/secrets); fail-closed §22.9; immutable. STRICT TDD: write the FAILING test first, confirm it fails, then fix. Use .venv (.venv/Scripts/python, .venv/Scripts/pip). Reuse existing models/clients; keep SQLite-dev/Postgres-prod portability. During iteration run ONLY your module's tests (other agents edit concurrently); the workflow runs the consolidated suite afterward.`
const REPORT = { type:'object', additionalProperties:false, required:['summary'], properties:{ summary:{type:'string'}, files_changed:{type:'array',items:{type:'string'}}, highs_fixed:{type:'number'}, tests_pass:{type:'boolean'} } }
const VERIFY = { type:'object', additionalProperties:false, required:['passed','summary'], properties:{ passed:{type:'boolean'}, summary:{type:'string'}, failing_tests:{type:'array',items:{type:'string'}} } }

phase('Fix')
const GROUPS = [
  { key:'daily', label:'fix:daily', own:'src/vision/cli/daily.py (and a new small ops helper if you need a lock)', tests:'tests/test_daily_orchestration.py',
    task:`Fix the high-severity Codex findings in Project VISION's DAILY ORCHESTRATION. You OWN src/vision/cli/daily.py (you may add a NEW helper file e.g. src/vision/ops/joblock.py for the lock). Read ${THREAT}.
KNOWN HIGHS:
1) daily.py ~721 / main ~761: the approval email is sent INSIDE run_daily BEFORE the enclosing get_session() transaction commits. If commit fails after send, the owner holds live approval links to a rolled-back/absent draft, and a retry double-sends + double-creates. FIX: commit the draft+tokens FIRST, THEN send the email, THEN record a durable email_sent marker (transactional-outbox / commit-before-send). Never emit the external side-effect inside an uncommitted transaction.
2) daily.py ~398-426 _send_approval_email: sender.send() may succeed then send_deduper.mark_sent() raises, and the outer try/except records email_sent=False + dedup unmarked → same-day retry DUPLICATE email. FIX: capture 'delivered' before mark_sent, guard mark_sent in its own try/except that logs but does not flip delivered, return the true delivered value.
3) daily.py ~555-673: NO overlap/concurrency lock (threat model checklist 'prevent overlapping cron runs with an atomic lock'), and each run unconditionally mints a NEW pending_approval draft + fresh single-use tokens keyed only implicitly on the day → overlapping/retried cron runs create multiple approvable drafts → duplicate posts. FIX: acquire an atomic per-job lock at the top of run_daily/main (a portable lockfile OR a DB row with a unique (job,date) constraint) and exit early if held; add an idempotency key (date + selected-item-set hash + focus) so a re-run REUSES the existing pending draft for the day instead of minting a new one.
ALSO: run  bash ~/.claude/council/codex_call.sh "Review src/vision/cli/daily.py for remaining high-severity fail-open/idempotency/no-double-post bugs, terse file:line" review 2 30  and fix any ADDITIONAL highs it reports (the review counted 5 highs here; make sure all are addressed).
${CONV}
RED tests first (tests/test_daily_orchestration.py): commit-failure-after-send does not orphan live tokens; mark_sent raising does not cause a duplicate send; a second concurrent run is blocked by the lock / reuses the same draft (no second token minted). Run .venv/Scripts/python -m pytest tests/test_daily_orchestration.py -q. Return files_changed, highs_fixed, tests_pass.` },

  { key:'cli_guards', label:'fix:cli-guards', own:'src/vision/cli/publisher.py + src/vision/cli/token.py', tests:'tests/test_publisher.py tests/test_token_refresh.py',
    task:`Fix the crash-loop / resource-leak high-severity Codex findings in Project VISION's cron entrypoints. You OWN src/vision/cli/publisher.py and src/vision/cli/token.py.
KNOWN HIGHS:
1) publisher.py ~41-55: no outer exception boundary — any DB/worker/reap exception escapes main() and dumps an unsanitized traceback (crash-loop-adjacent for a 5-min poller). Also LinkedInPublisher(settings) is constructed OUTSIDE the try/finally, so a partial-alloc-then-raise leaks the HTTP pool. FIX: wrap the body in try/except that logs a SANITIZED error (exception class + correlation id, never raw provider text) and returns 1 (fail-closed non-zero for cron alerting); move publisher construction inside try/finally so close() always runs.
2) token.py ~42-54: no crash-loop guard around refresh_if_needed/get_session — an unguarded exception can leak provider error text (this is the most secret-sensitive job). FIX: wrap in try/except, log exception class + correlation id ONLY, return 1 on unexpected failure. Mirror daily.main()'s fail-closed boundary.
ALSO run codex_call.sh review on both files and fix any additional highs. ${CONV}
RED tests first: an injected exception in the worker/refresh path yields exit code 1 + a sanitized log (no traceback, no provider text), and resources are closed. Run .venv/Scripts/python -m pytest tests/test_publisher.py tests/test_token_refresh.py -q. Return files_changed, highs_fixed, tests_pass.` },

  { key:'alerts', label:'fix:alerts', own:'src/vision/ops/alerts.py + src/vision/ops/feed_health.py', tests:'tests/test_alerting.py',
    task:`Fix the durable-dedup high-severity Codex finding in Project VISION's alerting. You OWN src/vision/ops/alerts.py and src/vision/ops/feed_health.py.
KNOWN HIGH: alerts.py ~220-260/323-338: the dedup/rate-limit state (_last_fired) is process-local in-memory; build_alerter() starts empty and each cron tick is a NEW process, so a persistent fault (dead feed, token reauth) RE-ALERTS EVERY TICK — spam, violating NFR-08 'actionable not noisy'. The docstrings overpromise durability the code lacks. FIX: persist last-fired timestamps durably (a small DB table keyed on dedup_key, OR a state file) so suppression survives process restarts; keep the in-memory map as an optimisation. Update the docstrings to match reality.
ALSO run codex_call.sh review on both files and fix any additional highs. ${CONV}
RED test first (tests/test_alerting.py): two SEPARATE alerter instances (simulating two cron ticks) for the same persistent fault within the window fire the channel only ONCE. Run .venv/Scripts/python -m pytest tests/test_alerting.py -q. Return files_changed, highs_fixed, tests_pass.` },

  { key:'deploy', label:'fix:deploy', own:'deploy/systemd/*, docker-compose.yml, deploy/deploy.sh, tests/test_deploy_smoke.py', tests:'tests/test_deploy_smoke.py',
    task:`Fix the deploy-hardening high-severity Codex findings in Project VISION. You OWN deploy/systemd/*, docker-compose.yml, deploy/deploy.sh, and tests/test_deploy_smoke.py.
KNOWN HIGHS:
1) vision-expire is a first-class scheduled job (in Dockerfile role dispatcher, crontab.example, DEPLOY.md 20:00 IST fail-closed expiry) but NO vision-expire.service/.timer ships in deploy/systemd/ and no vision-expire service in docker-compose.yml → under both deploy shapes un-actioned drafts are NEVER auto-expired (breaks fail-closed 'un-actioned => expire, never post'). FIX: add deploy/systemd/vision-expire.service + vision-expire.timer (20:00 IST, Persistent=true) mirroring the token unit, and a vision-expire job service in docker-compose.yml; add vision-expire to _EXPECTED_SERVICES / the role loop in tests/test_deploy_smoke.py.
2) vision-web.service ~37-38: StartLimitIntervalSec + StartLimitBurst are in [Service]; systemd only honors them in [Unit] → the restart-storm guard is INEFFECTIVE (crash-loops forever every 5s). FIX: move both into [Unit] (leave RestartSec in [Service]).
3) deploy/deploy.sh ~56-64: the post-deploy /healthz check is non-fatal — on non-200 it warns but exits 0, so a broken deploy reports SUCCESS. FIX: on health-check failure exit non-zero (and ideally roll back / restart), fail-closed.
ALSO run codex_call.sh review on these files and fix any additional highs. ${CONV}
RED tests first (tests/test_deploy_smoke.py): assert a vision-expire systemd unit + compose service exist; assert StartLimit directives live under [Unit]; assert deploy.sh exits non-zero on a simulated failed health check. Run .venv/Scripts/python -m pytest tests/test_deploy_smoke.py -q. Return files_changed, highs_fixed, tests_pass.` },
]
const fixes = (await parallel(GROUPS.map(g => () => agent(g.task, { label:g.label, schema:REPORT, phase:'Fix' }).then(r => ({...g, r}))))).filter(Boolean)
log(`Fix: ${fixes.map(f => f.key+'='+(f.r?.tests_pass?'green':'?')).join(' ')}`)

phase('Verify')
let verify = await agent(`Consolidated verify for Project VISION. In ${ROOT}: .venv/Scripts/python -m pytest -q and .venv/Scripts/ruff check src. Report pass/fail + failing tests (cross-module interactions from the parallel fixes). Do not fix.`, { label:'verify-consolidated', schema:VERIFY, phase:'Verify' })
let round = 0
while (!verify?.passed && round < 3) {
  round++; log(`Consolidated red — fix round ${round}`)
  await agent(`Full suite failing after Phase-4 fixes: ${JSON.stringify(verify?.failing_tests||verify?.summary)}. In ${ROOT}: diagnose + FIX root cause, re-run .venv/Scripts/python -m pytest -q until green. ${CONV} Return what changed.`, { label:`fix:round${round}`, phase:'Verify', agentType:'build-error-resolver' })
  verify = await agent(`Re-run in ${ROOT}: .venv/Scripts/python -m pytest -q + ruff check src. Report pass/fail + failing tests.`, { label:`verify:round${round}`, schema:VERIFY, phase:'Verify' })
}

phase('Security')
// Whole-codebase Codex security re-review of the FIXED code; fix remaining highs; re-verify.
const sec = await agent(
`Final adversarial SECURITY REVIEW of the FIXED Project VISION codebase (${ROOT}). Read ${THREAT}. Run:
  bash ~/.claude/council/codex_call.sh "Adversarially review Project VISION (src/vision + deploy) after hardening fixes. Can any post publish without a valid unexpired single-use approval? token replay/forgery? OAuth tokens unencrypted or logged? secrets in logs/CLI args/tracebacks? GET endpoints that mutate? cron overlap → double post? fail-open ordering? Give concrete file:line HIGH findings only." review 2 40
Read the flagged files yourself to confirm each is real. For every CONFIRMED high, FIX it via TDD (RED test first). Then run .venv/Scripts/python -m pytest -q + ruff check src until green. Report the findings, what you fixed, and final pass/fail.`,
  { label:'security-sweep', schema:VERIFY, phase:'Security' }
)

return {
  fixes: fixes.map(f => ({ group:f.key, highs_fixed:f.r?.highs_fixed, tests_pass:f.r?.tests_pass })),
  consolidated_passed: !!verify?.passed,
  security_sweep_passed: !!sec?.passed,
  security_summary: sec?.summary,
}

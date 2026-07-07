export const meta = {
  name: 'vision-council-stage2',
  description: 'Wire the Brahmastra Council into the autonomous engine — modularize the proven prototype, topic engine, vision-council CLI, approval email (Overrule), publish with Powered-by-Brahmastra',
  phases: [
    { title: 'Engine', detail: 'promote scripts/council.py into src/vision/council/ (voices+formats+topics+deliberate+compose+engine) + config + tests' },
    { title: 'Wire', detail: 'data model (council_meta) + vision-council CLI + mailer renders council draft + publish assembly with signature' },
    { title: 'Prove', detail: 'Codex review + pytest + LIVE dry-run: real council draft through the pipeline + approval-email preview' },
  ],
}

const ROOT = 'D:\\\\Projects\\\\ClaudeCode\\\\Vision-LinkedIN'
const PROTO = `${ROOT}\\\\scripts\\\\council.py`
const CONV = `CONVENTIONS (BRD §22): fully-commented WHY; type hints; specific exceptions, no bare except; logging (never log secrets); config over code; fail-closed. STRICT TDD where practical; MOCK the AI voices in unit tests (never call real models in a unit test). Use .venv (.venv/Scripts/python, .venv/Scripts/pip). Reuse existing proven code — do NOT rewrite the working prompts.
CRITICAL REUSE: the council prototype at ${PROTO} is PROVEN and owner-approved (de-named voices, 'Powered by Brahmastra' only, unnamed 'Council' block, honesty gate, format-variety engine, tonal range). PROMOTE its logic + prompts VERBATIM into the package — do not reword the prompts. All three voices run headless: Gemini via ~/.claude/council/agy_call.sh, Codex via ~/.claude/council/codex_call.sh, Claude via 'claude -p'. Invoke each through bash -c with the prompt as a POSITIONAL arg (\\$1) so quotes/newlines can't break the command (see the prototype's ask()).`

const MANIFEST = { type:'object', additionalProperties:false, required:['files','summary'], properties:{ files:{type:'array',items:{type:'object',additionalProperties:false,required:['path','purpose'],properties:{path:{type:'string'},purpose:{type:'string'}}}}, summary:{type:'string'}, tests_pass:{type:'boolean'}, notes:{type:'string'} } }
const REVIEW = { type:'object', additionalProperties:false, required:['module','verdict','issues'], properties:{ module:{type:'string'}, verdict:{type:'string',enum:['pass','pass_with_nits','needs_fix']}, issues:{type:'array',items:{type:'object',additionalProperties:false,required:['severity','description'],properties:{severity:{type:'string',enum:['high','medium','low']},file:{type:'string'},line:{type:'string'},description:{type:'string'},suggested_fix:{type:'string'}}}}, codex_ran:{type:'boolean'} } }
const VERIFY = { type:'object', additionalProperties:false, required:['passed','summary'], properties:{ passed:{type:'boolean'}, summary:{type:'string'}, failing_tests:{type:'array',items:{type:'string'}} } }

// ── Phase 1: the council package (foundation) ──
phase('Engine')
const engine = await agent(
`Build the COUNCIL ENGINE package for Project VISION by PROMOTING the proven prototype ${PROTO} into src/vision/council/. ${CONV}
Create these files (reuse the prototype's proven prompts + logic VERBATIM; refactor into clean modules, do not change wording of the deliberation/compose prompts):
- src/vision/council/__init__.py
- src/vision/council/voices.py — the 3 headless voices (gemini/codex/claude) with a uniform ask(voice, prompt)->str, timeout, clean UTF-8 capture, fail-soft per voice. Council dir + claude binary path configurable via settings (default '~/.claude/council' and 'claude'), expanduser'd (learn from the client.py council-dir bug: expand ~ and use posix paths for bash).
- src/vision/council/formats.py — the format-variety library (the prototype's FORMATS) + recent-format tracking. Persist recent formats in the DB (a tiny key/value or a 'council_state' row) OR a state file under a configurable path — NOT hard-coded to prep/. Avoid repeating the last ~4 formats.
- src/vision/council/topics.py — topic engine: (a) propose_topics(n) → the council proposes N novel, thought-provoking topics across ANY domain (tech, ethics, healthcare, leadership, culture, everyday-life, humour), (b) an editable EXCLUSION guardrail list from settings (topics not to touch), filtered out, (c) an owner topic QUEUE loaded from a configurable file (e.g. prep/council_topics.txt, one per line) consumed FIFO, (d) pick_topic(): owner-queue first, else propose+pick one that isn't a recent repeat. Mood/tone variety comes from compose (already handles it).
- src/vision/council/deliberate.py — the 2-round deliberation (prototype's deliberate()): round 1 independent takes, round 2 respond-to-each-other. Returns a Deliberation with round1/round2 per voice.
- src/vision/council/compose.py — the compose step VERBATIM from the prototype (de-named, honesty gate, 'Powered by Brahmastra' only, unnamed 'Council' block, tonal range). Parse out FORMAT / SITUATION / POST / COUNCIL sections into a structured result: {format, situation, post_text, council_block, hashtags}.
- src/vision/council/engine.py — run_council(topic=None) -> a Draft-shaped dict: {content_mode:'council', topic, format, situation, post_text, hashtags, council_block, transcript (the raw round1/round2), model_trace}. Orchestrates pick_topic -> deliberate -> compose. Fail-closed (a dead voice degrades; if <2 voices produce takes, raise).
- src/vision/config.py — ADD council settings: council_enabled, council_topic_queue_path, council_exclusions (list), council_claude_bin, council_recent_window. (Edit config.py additively; do not break existing settings.)
- tests/test_council.py — AAA, MOCK voices (patch the subprocess/ask so NO real model is called): assert deliberate builds 2 rounds, compose parses sections + strips AI names (assert 'Gemini'/'Codex'/'Claude' NOT in post_text/council_block), honesty-gate situations, format-variety avoids recent repeats, exclusion list filters topics, owner-queue consumed first, engine returns the Draft-shaped dict.
Return the manifest.`,
  { label:'council-engine', schema:MANIFEST, phase:'Engine' }
)
log(`Engine: ${engine?.files?.length ?? 0} files`)

// ── Phase 2: wire into the pipeline (parallel, disjoint files) ──
phase('Wire')
const [model, mailerPub] = await parallel([
  () => agent(
`Wire the council into the pipeline data + CLI for Project VISION. ${CONV} You OWN: src/vision/db/models.py (additive), a new migration, src/vision/cli/council.py, and pyproject.toml (add the vision-council entry).
1. models.py: add a nullable JSON column 'council_meta' to the drafts table (stores {topic, format, situation, council_block, transcript}) and a nullable 'content_mode' text column (default 'news'; council drafts set 'council'). Additive only; keep SQLite/Postgres portable. Add an Alembic migration under src/vision/db/migrations/versions/ for the two columns.
2. src/vision/cli/council.py (vision-council entry): run vision.council.engine.run_council() -> build a pending_approval Draft row (post_text, hashtags, content_mode='council', council_meta populated, confidence, token issued via existing approval.tokens, token_expires_at) committed via get_session(); then send the approval email via the mailer (respect VISION_ENV modes: dry_run=compose+store, NO send; staging=send to self; live=send). Reuse existing draft state machine + tokens. main() for cron. Fully commented.
3. pyproject.toml: add  vision-council = "vision.cli.council:main"  to [project.scripts].
4. tests/test_council_cli.py — AAA, everything mocked (engine, mailer, session in-memory): a run creates a pending_approval council draft with council_meta set and content_mode='council'; dry_run sends no email; the approval token is issued.
Return the manifest.`,
    { label:'council-model-cli', schema:MANIFEST, phase:'Wire' }
  ),
  () => agent(
`Extend the mailer + publish for COUNCIL drafts in Project VISION. ${CONV} You OWN: src/vision/mailer/composer.py (extend), and src/vision/publish/worker.py's post-assembly + src/vision/visuals/signature.py if needed (extend). Do NOT touch models.py or cli/ (another agent owns those).
1. mailer/composer.py: when a draft has content_mode=='council' (council_meta present), render the approval email to show: the POST, then a 'Council' block (the 3 unnamed viewpoints from council_meta.council_block), then a collapsible/linked 'raw debate' peek (council_meta.transcript, escaped), then the action buttons INCLUDING a new 'Overrule' button (in addition to Approve/Post-now/Edit/Reject). Reuse the existing themed shell + safe_url. Keep all dynamic values HTML-escaped.
2. Overrule action: treat 'overrule' as an edit-flow variant — the owner supplies a one-line counter-take; wire it so the edit page (or a note) captures it. For v1 you may map Overrule to the existing Edit endpoint with a labelled prompt 'Add your override:' — reuse the edit machinery, do not build a whole new endpoint. Add 'overrule' to the token VALID_ACTIONS / action allowlist if the token module gates actions (check approval/tokens.py) so an overrule link verifies.
3. publish assembly (worker.py): when publishing a council draft, assemble the final LinkedIn text = post_text + a blank line + the 'Council' block + '\\n\\nPowered by Brahmastra' (respect POST_SIGNATURE_MODE: if text_footer/both, ensure 'Powered by Brahmastra' appears exactly once; do not double-sign). News drafts unchanged.
4. tests: extend tests/test_mailer.py (council email renders post+Council+overrule button, names escaped) and tests/test_publisher.py (council publish text includes the Council block + a single 'Powered by Brahmastra'). Mock everything external.
Return the manifest.`,
    { label:'council-mailer-publish', schema:MANIFEST, phase:'Wire' }
  ),
])
log(`Wire: model-cli=${model?.files?.length ?? 0} mailer-pub=${mailerPub?.files?.length ?? 0}`)

// ── Phase 3: review + verify + live proof ──
phase('Prove')
const review = await agent(
`Review the COUNCIL Stage-2 work in Project VISION for correctness, name-leak safety (NO AI model names ever reach post_text/council_block/published text — only 'Powered by Brahmastra'), reuse, and conventions. Files across src/vision/council/, src/vision/cli/council.py, src/vision/mailer/composer.py, src/vision/publish/worker.py, src/vision/db/models.py + migration.
Get Codex's second opinion: bash ~/.claude/council/codex_call.sh "Review the VISION council modules for: AI model names leaking into published text (must never happen), unescaped user/model content in the approval email, the vision-council CLI creating a valid pending_approval draft + token, double-signing 'Powered by Brahmastra', and whether unit tests mock the real model voices. Terse, file:line." review 2 30  (codex_ran=false if it errors).
Merge findings. Report only; do not fix.`,
  { label:'review:council', schema:REVIEW, phase:'Prove' }
)
const highs = (review?.issues || []).filter(i => i.severity === 'high')

let verify = await agent(`Verify Project VISION council build. In ${ROOT}: .venv/Scripts/pip install -e ".[dev]", then .venv/Scripts/python -m pytest -q + .venv/Scripts/ruff check src. Report pass/fail + failing tests. Do not fix.`, { label:'verify', schema:VERIFY, phase:'Prove' })
let round = 0
while ((!verify?.passed || highs.length) && round < 2) {
  round++; log(`fix round ${round} (${highs.length} highs, tests_passed=${verify?.passed})`)
  await agent(`Fix the council Stage-2 issues in ${ROOT}. High-severity review findings: ${JSON.stringify(highs)}. Failing tests: ${JSON.stringify(verify?.failing_tests||verify?.summary)}. Diagnose + FIX (esp. any AI-name leak into published text, unescaped email content, double-signing). Re-run pytest until green. ${CONV} Return what changed.`, { label:`fix:round${round}`, phase:'Prove', agentType:'build-error-resolver' })
  verify = await agent(`Re-run in ${ROOT}: .venv/Scripts/python -m pytest -q + ruff check src. Report pass/fail + failing tests.`, { label:`verify:round${round}`, schema:VERIFY, phase:'Prove' })
  highs.length = 0
}

// LIVE proof: generate a real council draft through the pipeline (dry_run) + render the approval email.
const live = await agent(
`LIVE end-to-end proof of the council autonomous engine in ${ROOT}. Ensure .env has VISION_ENV=dry_run. Run the real vision-council flow ONCE (real AI voices — Gemini/Codex/Claude — this makes real calls, allow a few minutes) so it: generates a council post on an auto-picked topic, stores a pending_approval council draft with council_meta, and (dry_run) composes but does NOT send the approval email. Then render that draft's approval email HTML to prep/council_email_preview.html (via the mailer composer, mock sender) so the owner can open it. Confirm: the published-text assembly and the email contain NO AI model names (grep the post_text/council_block/preview for 'Gemini','Codex','Claude','GPT' — must be absent) and DO contain 'Powered by Brahmastra' exactly once.
Report: the generated post (first 400 chars), the chosen format, whether name-leak check passed, and the preview file path. If a real voice call fails transiently, retry once.`,
  { label:'live-dryrun', schema:VERIFY, phase:'Prove' }
)

return {
  engine_files: engine?.files?.map(f=>f.path) ?? [],
  review_verdict: review?.verdict,
  review_high: (review?.issues||[]).filter(i=>i.severity==='high'),
  tests_passed: !!verify?.passed,
  live_proof_passed: !!live?.passed,
  live_summary: live?.summary,
}

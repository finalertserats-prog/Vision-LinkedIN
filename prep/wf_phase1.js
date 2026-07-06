export const meta = {
  name: 'vision-phase1',
  description: 'Project VISION Phase 1 — ingest/curate/synthesise/visuals + team review + verify + integration',
  phases: [
    { title: 'Construct', detail: '5 parallel agents: ingest, curate, synthesise chain, visuals (card renderer), own-post dedup' },
    { title: 'Review', detail: 'per-module Codex + Claude review' },
    { title: 'Verify', detail: 'venv + pytest; build-error-resolver fixes red' },
    { title: 'Integrate', detail: 'run full ingest→curate→synthesise on fixtures (mocked models), assert grounded draft + rendered card' },
  ],
}

const ROOT = 'D:\\\\Projects\\\\ClaudeCode\\\\Vision-LinkedIN'
const BRD = `${ROOT}\\\\VISION_BRD_v1.md`
const FINALERT = 'D:\\\\Projects\\\\ClaudeCode\\\\_scratch\\\\finalert'
const CONV = `ENGINEERING CONVENTIONS (BRD §22 — MANDATORY):
- Fully-commented code (WHY not what); type hints on every signature; immutable updates; no bare except; specific exceptions; logging module (never print); pathlib; f-strings.
- Deterministic LLM contracts: passes return strict JSON validated against a pydantic schema; fail loudly on drift.
- Config over code: feeds/prompts/voice/thresholds editable via files/env (they are staged in ${ROOT}\\\\prep\\\\ — voice_profile.yaml, sources_seed.yaml, raft_prompts.md).
- DB is SQLite dev / Postgres prod — DB-agnostic SQLAlchemy (portable types) via the existing src/vision/db layer. Reuse existing models in src/vision/db/models.py (sources, items, runs, drafts, own_posts...). Do NOT redefine them.
- Brahmastra is used via the EXISTING src/vision/brahmastra/client.py BrahmastraClient (CLI mode, lanes gemini/codex/claude). In tests, MOCK BrahmastraClient — never call a real model in a unit test.
- Tests part of done: pytest AAA, mock external deps (network/subprocess), cover failure paths.
- REUSE finalert patterns where noted (read-only clone at ${FINALERT}): news_mapper/fetcher.py (feedparser + MD5 dedup), world_engine/ingestion.py (parallel fetch + fallback scoring), agents/sentiment_agent.py (keyword scorer). Adapt, do not import from finalert.
- Ingest must send a browser User-Agent (some feeds 403 a default UA). Verified live feeds are listed in prep/sources_seed.yaml (endpts needs browser UA; healthcareitnews/mobihealthnews may 403 server-side — mark and continue; The Batch & Anthropic RSS are dead — excluded; Jack Clark's Import AI https://jack-clark.net/feed/ is a working AI substitute).`

const MANIFEST = {
  type: 'object', additionalProperties: false, required: ['files', 'summary'],
  properties: {
    files: { type: 'array', items: { type: 'object', additionalProperties: false, required: ['path', 'purpose'], properties: { path: { type: 'string' }, purpose: { type: 'string' } } } },
    summary: { type: 'string' }, tests_pass: { type: 'boolean' }, notes: { type: 'string' },
  },
}
const REVIEW = {
  type: 'object', additionalProperties: false, required: ['module', 'verdict', 'issues'],
  properties: {
    module: { type: 'string' }, verdict: { type: 'string', enum: ['pass', 'pass_with_nits', 'needs_fix'] },
    issues: { type: 'array', items: { type: 'object', additionalProperties: false, required: ['severity', 'description'], properties: { severity: { type: 'string', enum: ['high', 'medium', 'low'] }, file: { type: 'string' }, line: { type: 'string' }, description: { type: 'string' }, suggested_fix: { type: 'string' } } } },
    codex_ran: { type: 'boolean' },
  },
}
const VERIFY = {
  type: 'object', additionalProperties: false, required: ['passed', 'summary'],
  properties: { passed: { type: 'boolean' }, summary: { type: 'string' }, failing_tests: { type: 'array', items: { type: 'string' } } },
}

// ── Construct: 5 disjoint modules in parallel ──
phase('Construct')
const MODULES = [
  { key: 'ingest', label: 'construct:ingest', task:
`Build the INGEST layer for Project VISION. Read BRD §12 (sourcing) at ${BRD} and adapt finalert's ${FINALERT}\\\\news_mapper\\\\fetcher.py + world_engine\\\\ingestion.py patterns. ${CONV}
Create ONLY these NEW files:
- src/vision/ingest/feeds.py — a FeedFetcher: fetch RSS (feedparser) and API sources (Hacker News firebaseio topstories→items) with a browser User-Agent, per-feed timeout, parallel fetch (ThreadPoolExecutor), graceful per-feed failure (log + continue, update source.last_ok_at semantics via a returned health dict). Returns normalised raw items.
- src/vision/ingest/normalise.py — normalise each raw entry to the items schema (title, url, source, published_at [parse with dateutil, tz-aware], summary, lane, content_hash [sha256 of normalised title+url], raw). Pure functions.
- src/vision/ingest/sources.py — load prep/sources_seed.yaml and upsert into the sources table (idempotent by name); a get_enabled_sources(session, lane=None) helper.
- tests/test_ingest.py — AAA, mock network (feedparser.parse and httpx) with fixture RSS/JSON; assert normalisation, content_hash stability, browser-UA sent, one dead feed doesn't kill the batch, HN API mapping. NO real network.
Add feedparser, python-dateutil to pyproject if missing. Return manifest.` },

  { key: 'curate', label: 'construct:curate', task:
`Build the CURATE layer for Project VISION. Read BRD §12.3 (scoring), §12.4 (dedup) at ${BRD}. ${CONV}
Create ONLY these NEW files:
- src/vision/curate/dedup.py — item-level dedup: exact URL, normalised-title fuzzy match (difflib ratio ≥ threshold), content_hash; plus cross-day "don't resurface an item used in a draft in last 14 days" (query items/drafts). Pure + a session-using variant.
- src/vision/curate/score.py — score = w_recency*recency(published_at) + w_authority*source.authority_weight + w_relevance*semantic_relevance(item, owner_topic_profile) + w_crosscut*bonus_if_bridges_HC_and_AI. Weights + owner_topic_profile from config/prep (keywords list ok for relevance — TF/keyword overlap, no API). recency = exponential decay over RECENCY_HOURS. Fully documented.
- src/vision/curate/select.py — select_top(items, k, per_lane_balance): dedup → score → pick top candidates ensuring both lanes represented; mark items.selected. Returns selected list + rationale.
- tests/test_curate.py — AAA: exact/near-dup removal, recency decay ordering, authority weighting, cross-cut bonus, top-k lane balance, cross-day suppression. No network.
Return manifest.` },

  { key: 'synthesise', label: 'construct:synthesise', task:
`Build the SYNTHESIS chain for Project VISION (BRD §13.1/§13.4/§13.5). Use the EXISTING src/vision/brahmastra/client.py BrahmastraClient. Prompts are staged in ${ROOT}\\\\prep\\\\raft_prompts.md and voice in prep/voice_profile.yaml. ${CONV}
Create ONLY these NEW files:
- src/vision/synthesise/schemas.py — pydantic models for each pass output (GenerateOut{hook,body,takeaway,hashtags,claims[]}, CritiqueOut{revised,change_log,voice_flags}, VerifyOut{grounded,unsupported,revised_post,grounding_pct,confidence}, ImageDecision{image_type,rationale,card_spec,illustration_prompt}). Strict, extra=forbid.
- src/vision/synthesise/prompts.py — load raft_prompts.md + voice_profile.yaml; render each pass prompt with runtime inputs (focus, items, voice). RAFT structure preserved.
- src/vision/synthesise/quality.py — build the quality_report jsonb (char_count, has_hook, grounding_pct, unsupported_claims, tone_flags [banned_phrases from voice], compliance_flags, hashtags, confidence) and the grounding gate (grounding_pct >= settings.GROUNDING_MIN_PCT for auto-eligibility). Pure functions over the pass outputs + source items.
- src/vision/synthesise/pipeline.py — orchestrate generate→critique→verify via BrahmastraClient (lanes from settings.MODEL_GENERATE/CRITIQUE/VERIFY; if a lane is unavailable, degrade to a single working lane with distinct prompts and record it in model_trace per BRD §13.0). Validate each output against its schema (fail loudly). Compute quality_report. Return a Draft-shaped dict (post_text assembled from hook+body+takeaway, hashtags, source_item_ids, quality_report, confidence, model_trace). Also call the image-decision pass.
- tests/test_synthesise.py — AAA with a MOCKED BrahmastraClient returning canned JSON for each pass; assert: schema validation, grounding gate pass/fail, banned-phrase tone flag, model_trace recorded, degraded-lane fallback path, quality_report shape matches BRD §14.4. NO real model calls.
Return manifest.` },

  { key: 'visuals', label: 'construct:visuals', task:
`Build the VISUALS lane for Project VISION (BRD §13.6, D8/D10 — precision-first). ${CONV}
Create ONLY these NEW files:
- src/vision/visuals/decide.py — image_decision(post, claims) -> {none|informative-card|concept-illustration} using the ImageDecision from the synthesis pass (accept it as input) with a safe default of 'none'; enforce: anything with numbers/words => informative-card (deterministic), never diffusion.
- src/vision/visuals/card_renderer.py — DETERMINISTIC renderer using Pillow (stat/quote cards) and matplotlib (simple bar/line charts) — NO headless browser. On-brand palette from settings.CARD_BRAND_PALETTE (navy=#0B1F3A, gold=#C9A24B). render_stat_card(spec)->PNG bytes and render_chart(spec)->PNG bytes at LinkedIn dims (1200x627 and 1200x1200). Optional discreet BRAHMASTRA logo watermark when POST_SIGNATURE_MODE in {card_watermark,both} (draw a wordmark if no logo file). Every number rendered must come from card_spec.datapoints (each carries a source_item_id) — assert presence.
- src/vision/visuals/illustrate.py — thin wrapper over the EXISTING BrahmastraImageClient for concept-illustration (text-free); on failure raise/return None so caller degrades to text-only. (Stub-friendly: no real gen in tests.)
- tests/test_visuals.py — AAA: stat card renders a valid PNG of exact dimensions containing the exact numbers (assert via image size + that render didn't raise + datapoints required), chart renders, decision defaults to none, watermark toggles by config, missing datapoint raises. Mock BrahmastraImageClient.
Add pillow, matplotlib to pyproject. Return manifest.` },

  { key: 'own_dedup', label: 'construct:own-dedup', task:
`Build the OWN-POST DEDUP memory for Project VISION (BRD §11.5, FR-18: no post semantically duplicating owner's own posts from last 90 days). No API keys — use a portable local embedding/similarity. ${CONV}
Create ONLY these NEW files:
- src/vision/curate/own_dedup.py — record_own_post(session, draft_id, post_urn, post_text, published_at) storing a lightweight embedding (TF-IDF vector or normalised token-frequency dict serialised to JSON — portable, no heavy deps; comment that pgvector + real embeddings are the prod upgrade). similarity(a_text, b_text)->float (cosine over the local vectors). check_against_own(session, candidate_text, days=90, threshold=settings.DEDUP_SIM_THRESHOLD) -> {max_similarity, pass, nearest_urn}.
- tests/test_own_dedup.py — AAA: near-duplicate exceeds threshold (fail), distinct text passes, 90-day window excludes older posts, empty history passes. Use in-memory SQLite via the existing conftest fixture. No network.
Return manifest.` },
]
const built = (await parallel(MODULES.map(m => () =>
  agent(m.task, { label: m.label, schema: MANIFEST, phase: 'Construct' }).then(r => ({ ...m, manifest: r }))
))).filter(Boolean)
log(`Construct: ${built.length}/${MODULES.length} modules built`)

// ── Review: Codex + Claude per module ──
phase('Review')
const reviews = (await parallel(built.map(m => () =>
  agent(
`Review the '${m.key}' module of Project VISION for correctness, BRD compliance, and conventions. Files: ${(m.manifest?.files||[]).map(f=>f.path).join(', ')}.
1. Read them. 2. Get Codex's second opinion (real teammate):
   bash ~/.claude/council/codex_call.sh "Review these VISION ${m.key} files for bugs, security, missing failure handling, non-idiomatic Python, and whether tests truly mock external deps. Terse, concrete, file:line. Files: ${(m.manifest?.files||[]).map(f=>f.path).join(' ')}" review 2 30
   (if Codex errors/times out set codex_ran=false, proceed with your own review).
3. Merge findings. Check especially: LLM outputs schema-validated & fail-loud, no real network/model in unit tests, grounding gate correct, deterministic card numbers all trace to a source_item_id, dedup thresholds sane. Report only; do not fix.`,
    { label: `review:${m.key}`, schema: REVIEW, phase: 'Review' }
  )
))).filter(Boolean)
const high = reviews.flatMap(r => (r.issues||[]).filter(i => i.severity === 'high'))
log(`Review: ${reviews.filter(r=>r.verdict!=='needs_fix').length}/${reviews.length} pass; ${high.length} high issues`)

// ── Verify: pytest + fix loop ──
phase('Verify')
let verify = await agent(
`Verify Project VISION Phase 1. In ${ROOT}: ensure .venv exists (python -m venv .venv if not), then .venv/Scripts/pip install -e ".[dev]" (installs new deps feedparser/pillow/matplotlib/dateutil), then .venv/Scripts/python -m pytest -q. Report pass/fail + failing tests. Do not fix, just report.`,
  { label: 'verify-pytest', schema: VERIFY, phase: 'Verify' }
)
let round = 0
while (!verify?.passed && round < 2) {
  round++
  log(`Verify red — fix round ${round}`)
  await agent(
`Phase 1 tests failing: ${JSON.stringify(verify?.failing_tests || verify?.summary)}. In ${ROOT}: diagnose + FIX root cause (missing deps in pyproject, import errors, portable-type/mock issues), re-run .venv/Scripts/python -m pytest -q until green. ${CONV} Return what changed.`,
    { label: `fix:round${round}`, phase: 'Verify', agentType: 'build-error-resolver' }
  )
  verify = await agent(`Re-run in ${ROOT}: .venv/Scripts/python -m pytest -q. Report pass/fail + failing tests.`, { label: `verify:round${round}`, schema: VERIFY, phase: 'Verify' })
}

// ── Integrate: end-to-end pipeline on fixtures with mocked models ──
phase('Integrate')
const integ = await agent(
`Write and run an INTEGRATION test proving Project VISION's Phase-1 pipeline works end-to-end offline. In ${ROOT}:
Create tests/test_integration_phase1.py that: (1) seeds a few fixture items across both lanes into in-memory SQLite (conftest fixture), (2) runs curate.select_top, (3) runs synthesise.pipeline with a MOCKED BrahmastraClient returning realistic canned generate/critique/verify JSON grounded in the fixture items, (4) asserts: a well-formed draft dict, grounding_pct==100 gate passes, quality_report matches BRD §14.4 shape, model_trace present, (5) runs the image-decision→card_renderer path for an informative-card spec and asserts a non-empty PNG with the exact fixture numbers required. Then run it: .venv/Scripts/python -m pytest tests/test_integration_phase1.py -q.
Also produce a human-readable sample draft: write a small scripts/demo_draft.py that prints a mocked draft + saves a rendered card PNG to prep/sample_card.png (mocked models, no network), and run it. ${CONV}
Return whether the integration test passes and the sample artifacts produced.`,
  { label: 'integrate-e2e', schema: VERIFY, phase: 'Integrate' }
)

return {
  modules_built: built.map(m => m.key),
  reviews: reviews.map(r => ({ module: r.module, verdict: r.verdict, high: (r.issues||[]).filter(i=>i.severity==='high').length, codex_ran: r.codex_ran })),
  high_severity_issues: high,
  unit_tests_passed: !!verify?.passed,
  integration_passed: !!integ?.passed,
  integration_summary: integ?.summary,
}

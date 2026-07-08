export const meta = {
  name: 'vision-images',
  description: 'Wire images into the council: agy-based AI concept-illustrations + deterministic quote cards, into the approval email + LinkedIn publish; + video architecture',
  phases: [
    { title: 'Engine', detail: '2 agents: BrahmastraImageClient (agy agent path) + deterministic quote-card renderer' },
    { title: 'Wire', detail: '2 agents: council image lane (decide→generate→attach) + email/publish image path' },
    { title: 'Prove', detail: 'Codex review + verify + live real-agy image smoke + video architecture doc' },
  ],
}

const ROOT = 'D:\\\\Projects\\\\ClaudeCode\\\\Vision-LinkedIN'
const AGY = '/c/Users/vishn/AppData/Local/agy/bin/agy'
const CONV = `CONVENTIONS (BRD §22): fully-commented WHY; type hints; specific exceptions, no bare except; logging (never log secrets); config over code; fail-closed. STRICT TDD; MOCK subprocess/image-gen and network in unit tests (never call agy or LinkedIn in a unit test). Use .venv. Reuse existing code; do not rebuild the LinkedIn upload/publish (they exist).
CONFIRMED-WORKING AI IMAGE PATH (verified live 2026-07-08): agy (Antigravity/Gemini, the owner's subscription — NO API key) generates+saves real PNGs as an AGENT. Exact invocation that produced a valid 1024x1024 PNG:
  "${AGY}" --add-dir <ABS_CWD> --dangerously-skip-permissions -p "Use your image generation capability to create an image: <PROMPT>. Save the generated PNG to <ABS_OUTPUT_PATH>. Confirm the file path when done."
It writes the file to <ABS_OUTPUT_PATH>; the caller then reads the bytes. The legacy 'gemini' CLI is DEAD (IneligibleTierError) and gemini_image.sh does NOT work — do NOT use them. Codex image is account-rejected. So agy is THE AI-image path.
PRECISION RULE (BRD §13.6/D10): anything with NUMBERS or WORDS is rendered DETERMINISTICALLY (Pillow/matplotlib cards) — NEVER through agy/diffusion (they mangle text/figures). agy is for TEXT-FREE concept illustrations only; its style prompt MUST demand 'no text, no words, no letters, no logos'.`

const MANIFEST = { type:'object', additionalProperties:false, required:['files','summary'], properties:{ files:{type:'array',items:{type:'object',additionalProperties:false,required:['path','purpose'],properties:{path:{type:'string'},purpose:{type:'string'}}}}, summary:{type:'string'}, tests_pass:{type:'boolean'}, notes:{type:'string'} } }
const REVIEW = { type:'object', additionalProperties:false, required:['module','verdict','issues'], properties:{ module:{type:'string'}, verdict:{type:'string',enum:['pass','pass_with_nits','needs_fix']}, issues:{type:'array',items:{type:'object',additionalProperties:false,required:['severity','description'],properties:{severity:{type:'string',enum:['high','medium','low']},file:{type:'string'},line:{type:'string'},description:{type:'string'},suggested_fix:{type:'string'}}}}, codex_ran:{type:'boolean'} } }
const VERIFY = { type:'object', additionalProperties:false, required:['passed','summary'], properties:{ passed:{type:'boolean'}, summary:{type:'string'}, failing_tests:{type:'array',items:{type:'string'}} } }

phase('Engine')
const eng = (await parallel([
  () => agent(
`Rewrite the AI image client to use the CONFIRMED-WORKING agy path for Project VISION. ${CONV}
Edit src/vision/brahmastra/image_client.py: BrahmastraImageClient.illustrate(prompt, model=None) -> bytes MUST now:
- Build a text-free style-guided prompt (prepend the settings.IMAGE_STYLE_GUIDE + 'no text, no words, no letters, no logos').
- Run agy as an agent (binary path from a new setting AGY_BIN default '${AGY}', configurable) with: --add-dir <abs project cwd> --dangerously-skip-permissions -p "Use your image generation capability to create an image: <styled prompt>. Save the generated PNG to <abs temp output>. Confirm the file path when done." Use a tempfile for the output.
- After agy exits, READ the saved PNG bytes, validate it's a real image (PNG/JPEG magic; if agy saved JPEG, load+convert to PNG via Pillow), and return the bytes. If no valid file was produced, raise ImageGenerationError (caller degrades to text-only, never blocks publishing — BRD §13.6). Timeout ~200s, one retry.
- Add AGY_BIN to src/vision/config.py.
Update tests/test_brahmastra_client.py (image tests): MOCK subprocess.run so agy is never really called; simulate agy writing a valid PNG to the temp path (patch so the file appears) → illustrate returns bytes; simulate no-file → ImageGenerationError; assert 'no text/no logos' is in the prompt. Return manifest.`,
    { label:'image-client-agy', schema:MANIFEST, phase:'Engine' }
  ),
  () => agent(
`Build a DETERMINISTIC quote-card renderer for Project VISION (the reliable, no-model visual for idea-driven council posts). ${CONV}
Add to src/vision/visuals/card_renderer.py a new function render_quote_card(quote: str, *, attribution: str | None = None, settings=None) -> bytes: a 1200x1200 (and/or 1200x627) on-brand navy/gold card (palette from settings.CARD_BRAND_PALETTE) with the quote text rendered by Pillow — word-wrapped, auto-fit font size, centered, with a tasteful gold accent (a rule or opening quote mark). NO grounding/datapoint requirement (unlike stat cards; a quote is prose, not numbers). Optional discreet watermark per POST_SIGNATURE_MODE (reuse the existing watermark logic). Reuse the existing palette parsing / fonts / fallback-on-bad-color helpers (learn from the prior card_renderer bugs: validate palette, wrap+truncate text so it never overflows the canvas).
Add tests to tests/test_visuals.py: renders a valid PNG of exact dimensions for a short and a long quote (long one wraps/truncates within bounds), bad palette degrades to defaults (no crash), watermark toggles by config. Return manifest.`,
    { label:'quote-card', schema:MANIFEST, phase:'Engine' }
  ),
])).filter(Boolean)
log(`Engine: ${eng.map(e=>e?.files?.length??0).join('+')} files`)

phase('Wire')
const wire = (await parallel([
  () => agent(
`Wire the IMAGE LANE into the COUNCIL engine for Project VISION. ${CONV} Reuse the existing src/vision/visuals/decide.py, card_renderer.render_quote_card, brahmastra/image_client.py (agy), and the draft image_* columns (image_type/image_path/image_source/image_prompt).
In src/vision/council/engine.py (and a new src/vision/council/visual.py if cleaner): after compose, run a COUNCIL image-decision that returns one of: 'none' | 'quote_card' | 'concept_illustration'. Council posts are ideas/opinions (rarely numbers), so the sensible default policy: pick 'quote_card' when the post has a strong one-line punchline (render_quote_card with that line), OR 'concept_illustration' (agy, text-free) for a more atmospheric post, OR 'none'. Make the choice + which line/prompt config-driven and NOT every post (respect settings.IMAGE_MAX_PER_WEEK and a simple heuristic/rotation). Generate the chosen image, write it to a file under a configured images dir, and set draft.image_type/image_path/image_source/image_prompt on the council draft dict so the mailer + publisher pick it up. Image-gen failure MUST degrade to text-only (never block). Add tests (mock render_quote_card + BrahmastraImageClient — no real agy/network): each decision path sets the right image fields; failure → image_type 'none'; weekly cap respected. Return manifest.`,
    { label:'council-image-lane', schema:MANIFEST, phase:'Wire' }
  ),
  () => agent(
`Ensure the approval EMAIL shows the council image AND the PUBLISHER attaches it, for Project VISION. ${CONV} You OWN checks/edits in src/vision/mailer/composer.py and src/vision/publish/worker.py (image path only). Reuse existing image_upload / LinkedInClient.upload_image / publish_with_image.
1. composer.py: when a draft has image_type != 'none' and an image_path, EMBED the image preview in the approval email (inline/attached) so the owner proof-reads it (regenerate/drop stays a future nicety). Escape all text.
2. worker.py publish path: when publishing a COUNCIL draft that has an approved image, upload it via the existing image path and publish_with_image (attach the image URN); text still assembled by _compose_council_text (body only, no signature per current setting). Image failure degrades to text-only (never blocks).
Add/extend tests (mock LinkedInClient + image read; no network): council draft WITH image → publish_with_image called with an image URN; email HTML contains the image; image read failure → falls back to text publish. Return manifest.`,
    { label:'email-publish-image', schema:MANIFEST, phase:'Wire' }
  ),
])).filter(Boolean)
log(`Wire: ${wire.map(w=>w?.files?.length??0).join('+')} files`)

phase('Prove')
const review = await agent(
`Review the COUNCIL IMAGE work in Project VISION for correctness + the PRECISION rule (numbers/words => deterministic card, NEVER agy/diffusion) + graceful degradation (image failure never blocks publishing). Files: image_client.py, card_renderer.py, council/*, mailer/composer.py, publish/worker.py.
Codex second opinion: bash ~/.claude/council/codex_call.sh "Review VISION council-image code: does any numeric/text content get sent to the diffusion (agy) path instead of a deterministic card? can an image-gen or upload failure block the text post (it must NOT)? is the agy subprocess invocation safe (no shell injection of the prompt)? are unit tests mocking agy+network? terse file:line." review 2 30 (codex_ran=false if it errors). Merge. Report only.`,
  { label:'review:images', schema:REVIEW, phase:'Prove' }
)
const highs = (review?.issues||[]).filter(i=>i.severity==='high')
let verify = await agent(`Verify Project VISION images build. In ${ROOT}: .venv/Scripts/pip install -e ".[dev]", then .venv/Scripts/python -m pytest -q + ruff check src. Report pass/fail + failing tests.`, { label:'verify', schema:VERIFY, phase:'Prove' })
let round = 0
while ((!verify?.passed || highs.length) && round < 2) {
  round++; log(`fix round ${round}`)
  await agent(`Fix Project VISION image issues in ${ROOT}. Highs: ${JSON.stringify(highs)}. Failing: ${JSON.stringify(verify?.failing_tests||verify?.summary)}. ${CONV} Re-run pytest until green. Return changes.`, { label:`fix:${round}`, phase:'Prove', agentType:'build-error-resolver' })
  verify = await agent(`Re-run in ${ROOT}: pytest -q + ruff check src. pass/fail + failing.`, { label:`verify:${round}`, schema:VERIFY, phase:'Prove' })
  highs.length = 0
}

// Live smoke + video architecture, in parallel.
const [smoke, videodoc] = await parallel([
  () => agent(
`LIVE image smoke for Project VISION in ${ROOT} (real agy — makes a real subscription image call, ~2 min). Call BrahmastraImageClient().illustrate("a calm abstract flow of light and quiet geometry") directly and assert it returns real image bytes that Pillow opens as a valid image with sane dimensions; save it to prep/imgtest/live_concept.png. Then render_quote_card("The distortion isn't the bug. It might be the whole point.") and save prep/imgtest/live_quote.png. Report both file sizes + dimensions + whether the agy image is valid. If agy transiently fails, retry once; if it still fails report that honestly (do not fake).`,
    { label:'live-image-smoke', schema:VERIFY, phase:'Prove' }
  ),
  () => agent(
`Write the VIDEO / VOICE-OVER / MUSIC architecture for Project VISION as docs/VIDEO_ARCHITECTURE.md (design only, no heavy impl). Get Codex's input: bash ~/.claude/council/codex_call.sh "Outline the module layout + pipeline for adding short vertical 'Insight Reels' to a LinkedIn engine: deterministic motion-graphics for exact numbers, text-free Veo 3.1 B-roll, TTS voice-over, music, burned-in captions, ffmpeg assembly, LinkedIn /rest/videos chunked upload (initializeUpload->ETag parts->finalizeUpload->attach URN). Terse." default 2 30 (if it errors, proceed from your own knowledge + BRD §23). Cover: pipeline stages, a proposed src/vision/video/ module layout with responsibilities (script, motion_graphics, broll (Veo), voiceover (TTS), music, assemble (ffmpeg), upload (LinkedIn /rest/videos)), the LinkedIn video upload flow, precision+authenticity guardrails (no fabricated numbers in generative video, captions always, human approval, opt-in/weekly not daily), honest 2026 tooling (Veo alive; Sora API shuts 24 Sep 2026 — do NOT build on it; verify model IDs at build time; image gen proven via agy), and a phased plan (5a carousel, 5b motion-graphics reel + TTS, 5c Veo B-roll + voice clone). Return a short summary of what you wrote + the file path.`,
    { label:'video-architecture', schema:VERIFY, phase:'Prove' }
  ),
])

return {
  engine: eng.flatMap(e=>e?.files?.map(f=>f.path)||[]),
  review_verdict: review?.verdict, review_high: (review?.issues||[]).filter(i=>i.severity==='high'),
  tests_passed: !!verify?.passed,
  live_image_ok: !!smoke?.passed, live_summary: smoke?.summary,
  video_arch: videodoc?.summary,
}

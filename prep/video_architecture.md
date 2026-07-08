# Project VISION Video Architecture: Insight Reels

_As of 2026-07-08. Current-source checks: LinkedIn Videos/Posts API docs, Google Veo docs, OpenAI deprecation docs._

## 1. Pipeline: Approved Council Post → Vertical Insight Reel

```text
approved_post
  → video_intent_gate
  → script + storyboard
  → factual asset plan
  → deterministic motion graphics
  → optional text-free Veo B-roll
  → TTS voice-over
  → licensed background music
  → captions + transcript
  → ffmpeg assembly
  → QC + human approval
  → LinkedIn video upload
  → LinkedIn post publish
```

### Stages

1. **Video intent gate**
   - Default: `video_enabled: false`.
   - Opt-in per post or weekly flagship only; not daily.
   - Requires already-approved council post.
   - Reject if post lacks a clear visual narrative or contains sensitive claims needing fresh verification.

2. **Script + storyboard**
   - Convert post into 30–60s vertical reel.
   - Outputs:
     - `voiceover_script`
     - `scene_list`
     - `caption_segments`
     - `motion_graphics_requirements`
     - `broll_prompts`
   - All claims/numbers must trace to the approved post’s source bundle.

3. **Deterministic motion graphics**
   - Use for **all exact numbers, charts, quotes, names, company names, dates, percentages, and visible text**.
   - Render via deterministic stack: SVG/HTML canvas/Remotion-style frames → PNG sequence/video layer.
   - No generative model may render factual text or numeric graphics.

4. **Text-free Veo B-roll**
   - Use only for atmosphere: office, abstract markets, city, collaboration, technology ambience.
   - Prompt contract must include:
     - `no text`
     - `no logos`
     - `no charts`
     - `no numbers`
     - `no recognizable real people unless licensed`
   - Veo is currently viable; Google states Veo 3 supports vertical `9:16` and uses SynthID watermarking for generated video. Sources: [Google Veo 3 GA/pricing update](https://developers.googleblog.com/en/veo-3-and-veo-3-fast-new-pricing-new-configurations-and-better-resolution/), [Veo 3 Gemini API launch](https://developers.googleblog.com/veo-3-now-available-gemini-api/).

5. **TTS voice-over**
   - Provider adapters:
     - ElevenLabs
     - OpenAI audio/TTS
     - Google Cloud TTS / Gemini speech stack
   - Voice clone only with explicit consent, stored consent artifact, and per-post approval.
   - Output: WAV 48kHz preferred for editing; normalize loudness before final mix.

6. **Background music**
   - Only licensed, owned, or royalty-cleared tracks.
   - Maintain `music_license_id`, source, allowed platforms, expiry.
   - Auto-duck under narration.

7. **Burned-in captions**
   - Always on.
   - Generate `.srt`/`.vtt` plus burned-in subtitle layer.
   - Captions must match final voice-over timing.
   - Use high-contrast LinkedIn-safe layout; avoid lower UI collision.

8. **Final MP4 assembly**
   - `ffmpeg` owns final mux/render:
     - 9:16 vertical
     - H.264 MP4
     - AAC audio
     - burned-in captions
     - normalized loudness
     - thumbnail extraction
   - Store render manifest with hashes of all inputs.

9. **QC + human approval**
   - Automated checks:
     - duration
     - aspect ratio
     - file size
     - silence/clipping
     - captions present
     - no generated text layer detected in B-roll metadata
     - all factual overlays trace to source bundle
   - Human approval required before upload.

---

## 2. Module Layout

```text
src/vision/video/
  index.ts
  types.ts
  config.ts

  pipeline/
    createInsightReel.ts
    runVideoJob.ts
    stages.ts
    manifests.ts

  planning/
    scriptPlanner.ts
    storyboardPlanner.ts
    factualAssetPlanner.ts
    promptBuilder.ts

  guardrails/
    factTraceValidator.ts
    generativeVideoPolicy.ts
    authenticityPolicy.ts
    consentValidator.ts
    musicLicenseValidator.ts
    approvalGate.ts

  motion/
    motionSpec.ts
    renderMotionGraphics.ts
    chartRenderer.ts
    typography.ts
    templates/
      numberCard.ts
      quoteCard.ts
      timelineCard.ts

  broll/
    brollPlanner.ts
    veoClient.ts
    veoModelVerifier.ts
    brollModeration.ts

  voice/
    ttsClient.ts
    elevenLabsProvider.ts
    openAiTtsProvider.ts
    googleTtsProvider.ts
    voiceConsentStore.ts

  music/
    musicSelector.ts
    musicLibrary.ts
    ducking.ts

  captions/
    transcriptAligner.ts
    captionRenderer.ts
    srtWriter.ts
    vttWriter.ts

  assembly/
    ffmpegCommandBuilder.ts
    assembleMp4.ts
    thumbnailExtractor.ts
    loudnessNormalize.ts

  linkedin/
    linkedInVideoUpload.ts
    linkedInPostVideoPublisher.ts
    uploadParts.ts

  qc/
    probeMedia.ts
    validateFinalMp4.ts
    visualDiff.ts
    report.ts

  storage/
    artifactStore.ts
    renderCache.ts
    hash.ts
```

### Key Responsibilities

- `planning/`: turns approved post into script, storyboard, prompts, and traceable scene plan.
- `guardrails/`: fail-closed policy checks before generation, before assembly, and before publish.
- `motion/`: deterministic factual visuals.
- `broll/`: generative atmosphere only.
- `voice/`: TTS abstraction and consent enforcement.
- `music/`: licensed music selection and ducking.
- `captions/`: transcript alignment and burned-in caption assets.
- `assembly/`: reproducible `ffmpeg` final render.
- `linkedin/`: upload video, finalize, attach video URN to post.
- `qc/`: machine checks before human approval.

---

## 3. LinkedIn Video Upload Flow

LinkedIn’s current Videos API flow is:

1. **Initialize upload**
   - `POST https://api.linkedin.com/rest/videos?action=initializeUpload`
   - Headers:
     - `Authorization: Bearer ...`
     - `Linkedin-Version: YYYYMM`
     - `X-Restli-Protocol-Version: 2.0.0`
   - Body includes owner, file size, optional captions/thumbnail flags.
   - Response returns:
     - `video` URN
     - `uploadToken`
     - `uploadInstructions[]`
     - optional caption/thumbnail upload URLs

2. **Upload chunks**
   - Follow `uploadInstructions` exactly.
   - Each instruction provides:
     - `firstByte`
     - `lastByte`
     - `uploadUrl`
   - Upload each byte range with HTTP `PUT`.
   - Capture each response `ETag`.
   - Preserve ETag order matching `uploadInstructions`.

3. **Finalize upload**
   - `POST /rest/videos?action=finalizeUpload`
   - Send:
     - `video`
     - `uploadToken`
     - `uploadedPartIds`: ordered ETags, usually without surrounding quotes depending on client normalization.

4. **Attach video to post**
   - `POST https://api.linkedin.com/rest/posts`
   - `content.media.id = "urn:li:video:{id}"`
   - Include title/commentary/distribution/lifecycle state.
   - LinkedIn docs confirm video posts use a Video URN from the Videos API. Sources: [LinkedIn Videos API](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/videos-api?view=li-lms-2026-06), [LinkedIn Posts API](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api?view=li-lms-2026-06).

---

## 4. Precision + Authenticity Guardrails

- **No fabricated numbers**
  - Generative video cannot create charts, metrics, dates, UI screenshots, logos, or text.
  - All factual visuals come from deterministic render specs.

- **Traceability**
  - Every number/text overlay must map to:
    - approved post claim ID
    - source URL/document
    - reviewer approval ID

- **SynthID awareness**
  - Preserve provider metadata where possible.
  - Treat Veo output as synthetic and label internally.
  - Do not crop/re-encode solely to obscure watermarking.

- **Captions always**
  - Burned-in captions required.
  - Sidecar `.srt`/`.vtt` stored for audit and potential LinkedIn captions upload.

- **Human approval always**
  - Approval screen must show:
    - final MP4
    - transcript
    - script
    - factual trace table
    - music license
    - generated B-roll prompts
    - provider/model IDs

- **Frequency**
  - Opt-in per post.
  - Recommended cadence: weekly flagship reel.
  - Avoid auto-converting every post.

- **Voice authenticity**
  - No voice clone without explicit consent.
  - Synthetic voice disclosure policy configurable by brand/legal.

---

## 5. 2026 Tooling Reality

- **Veo is alive**
  - Use Veo for optional atmospheric B-roll.
  - Current Google docs/blogs describe Veo 3 / Veo 3 Fast as available through Gemini API, with vertical support and SynthID watermarking.

- **Do not build on Sora**
  - OpenAI states Sora 2 video generation models and the Videos API are deprecated and shut down on **2026-09-24**. Sources: [OpenAI deprecations](https://developers.openai.com/api/docs/deprecations), [OpenAI video generation guide](https://developers.openai.com/api/docs/guides/video-generation), [OpenAI Sora discontinuation help](https://help.openai.com/en/articles/20001152-what-to-know-about-the-sora-discontinuation).

- **Verify model IDs at build time**
  - Never hardcode long-lived assumptions like `veo-3.0-*` or TTS model IDs without validation.
  - Add CI/startup check:
    - query provider model registry or perform dry-run capability check
    - verify required capabilities: vertical video, no-text prompt policy, TTS output format
    - fail closed if unavailable
  - Store selected provider/model/version in render manifest.

---

## 6. Phased Build Plan

### Phase 5a — Carousel Foundation

- Build factual scene planner from approved posts.
- Generate deterministic LinkedIn carousel/image-card sequence.
- Add source trace table and human approval UI extensions.
- No generative video yet.

### Phase 5b — Motion-Graphics Reel + TTS

- Add `src/vision/video/` core pipeline.
- Deterministic motion graphics only.
- TTS narration.
- Licensed background music.
- Burned-in captions.
- `ffmpeg` assembly.
- LinkedIn video upload + post attach.
- Weekly flagship opt-in.

### Phase 5c — Veo B-roll + Voice Clone

- Add Veo atmospheric B-roll adapter.
- Enforce text-free/no-facts/no-logos generative policy.
- Add model verification.
- Add SynthID/metadata preservation notes in manifest.
- Add consent-backed voice clone support.
- Expand QC with B-roll prompt audit and voice consent validation.

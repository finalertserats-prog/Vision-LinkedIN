# VIDEO / VOICE-OVER / MUSIC Architecture — Project VISION

> **Status:** Design only (no heavy implementation). Post-core, opt-in. Traces to **BRD §23** (Expressive Media & Roadmap Suggestions) and the §22 Engineering Conventions.
>
> **One-line thesis:** An *Insight Reel* is a 20–45s vertical (1080×1920) video where **every factual pixel is deterministic** (exact numbers, charts, captions rendered by us) and generative AI (Veo B-roll, TTS voice) only adds **text-free motion and warmth**. Expressive *and* precise. Weekly flagship or per-post opt-in — **never forced daily**.

---

## 0. Where this fits in VISION

The existing pipeline is: `ingest → curate → synthesise → visuals (card) → approval → publish (LinkedIn)`. Video is a **parallel enrichment lane** that hangs off an already-approved insight and reuses the same **fail-closed approval gate** and the same **LinkedIn publish flow**. It does not replace the daily text/card path; it is a "promote to reel" action on the day's flagship insight.

```
                 approved insight (facts, numbers, voice draft)
                                   │
                    ┌──────────────┴──────────────┐
                    │        VIDEO LANE            │
   script → motion_graphics ─┐                     │
                    broll ───┤→ assemble (ffmpeg) → captions burned in
              voiceover ─────┤                     │
                  music ─────┘                     │
                                   │
                          approval (preview) ── fail-closed
                                   │
                        upload (LinkedIn /rest/videos) → attach URN → post
```

---

## 1. Pipeline stages

Each stage has a **deterministic contract** (strict JSON in / typed artifact out, §22.5) and is independently testable. Artifacts are written to a per-reel working dir with a **render manifest** (inputs, prompts, model IDs, content hashes, output paths) so any reel is reproducible and auditable.

| # | Stage | Input | Output | Determinism |
|---|-------|-------|--------|-------------|
| 1 | **Script** | Approved insight + owner-voice profile | `ReelScript` (scenes, narration, on-screen claims, timings) | LLM (Brahmastra), RAFT prompt, strict-JSON validated |
| 2 | **Motion graphics** | `ReelScript` claims + numbers | Deterministic overlay clips (numbers, charts, lower-thirds) with alpha | **Fully deterministic** — the precision layer |
| 3 | **B-roll (Veo)** | Text-free scene prompts | Ambient 9:16 clips (no text/logos/UI/faces) | Generative, atmosphere only, safety-guarded |
| 4 | **Voice-over (TTS)** | Narration text | VO audio + word-level timing metadata | Generative audio, aligned deterministically |
| 5 | **Music** | Mood tag + duration | Licensed bed, loudness-tagged | Selected from a curated licensed library |
| 6 | **Captions** | Narration + VO timing | Burned-in ASS/SRT styled to brand | **Deterministic** (from our transcript, never ASR-guessed) |
| 7 | **Assemble (ffmpeg)** | All above | Web-safe MP4 (H.264/AAC, 1080×1920, 30fps, faststart) | Deterministic composition + audio mix |
| 8 | **Approval** | Rendered MP4 + manifest | Human decision | Fail-closed (§22.9) |
| 9 | **Upload** | Approved MP4 | LinkedIn video URN (state `AVAILABLE`) | Resumable state machine |
| 10 | **Attach & post** | Video URN + copy | Published `urn:li:share`/`ugcPost` | Reuses existing publish flow |

**Stage ordering note:** VO (4) is synthesized **before** captions (6) and assembly (7) because caption timing and music ducking both key off the VO's word-level timestamps. Motion graphics (2) and B-roll (3) can render in parallel with VO.

---

## 2. Module layout — `src/vision/video/`

Consistent with the existing Python codebase (`src/vision/{ingest,curate,synthesise,visuals,approval,publish}`), fully-commented, config-over-code (§22.3, §22.6). File names are snake_case Python modules.

```
src/vision/video/
  __init__.py
  schema.py            # Pydantic contracts: ReelSpec, ReelScript, Scene,
                       #   RenderManifest, RenderJob, UploadResult, VideoAsset.
                       #   Single source of truth for every stage boundary.
  orchestrator.py      # Public entry point. Drives stages 1→10 as a resumable
                       #   state machine; writes/updates the render manifest;
                       #   NEVER blocks the daily text path.

  script.py            # STAGE 1 — Reel scripting.
                       #   RAFT prompt (role: "healthcare-tech reel director")
                       #   over the approved insight → strict-JSON ReelScript.
                       #   Separates NARRATION (spoken) from ON-SCREEN CLAIMS
                       #   (numbers/labels that MUST be deterministic).
                       #   Fails loudly on schema drift (§22.5).

  motion_graphics.py   # STAGE 2 — THE PRECISION LAYER.
                       #   Renders every exact number, chart, stat and lower-
                       #   third as deterministic transparent overlay clips
                       #   (SVG/canvas → PNG sequence or headless render).
                       #   No LLM/diffusion touches a single digit here.
                       #   Reuses visuals/style_guide + brand kit (fonts,
                       #   colors, logo, safe margins).

  broll.py             # STAGE 3 — Veo 3.1 text-free B-roll.
                       #   Builds prompts that HARD-forbid text/logos/UI/faces;
                       #   calls Veo via the Brahmastra adapter; validates each
                       #   returned clip (duration, 9:16, no-text heuristic).
                       #   Optional lane — reel renders fine without it.

  voiceover.py         # STAGE 4 — TTS voice-over.
                       #   Synthesizes narration (ElevenLabs / OpenAI TTS /
                       #   Google TTS) through the Brahmastra adapter; returns
                       #   audio + word-level timing used by captions + ducking.
                       #   Optional owner voice clone is CONSENT-GATED (§4).

  music.py             # STAGE 5 — Music bed.
                       #   Selects a LICENSED track from a curated library by
                       #   mood/duration; returns path + target loudness. No
                       #   AI music generation in v1 (licensing clarity).

  captions.py          # STAGE 6 — Captions (always on).
                       #   Turns our OWN narration transcript + VO timing into
                       #   styled ASS/SRT, burned into the frame by ffmpeg.
                       #   Never relies on the platform sidecar alone.

  assemble.py          # STAGE 7 — ffmpeg assembly.
                       #   Composits B-roll (bg) → motion graphics (fg) →
                       #   captions; mixes VO (primary) + ducked music; LUFS-
                       #   normalizes; exports 1080×1920 H.264/AAC MP4 with
                       #   +faststart (moov atom at front). Deterministic.

  upload.py            # STAGE 9 — LinkedIn /rest/videos.
                       #   initializeUpload → PUT byte-range parts (collect
                       #   ETags, in order) → finalizeUpload → poll status until
                       #   AVAILABLE. Resumable, idempotent, retry-with-backoff.
                       #   Attach step (10) reuses publish/linkedin.py to post.

  brand_kit.py         # Shared: colors, fonts, logo, lower-third rules, safe
                       #   margins, intro sting. Config-driven (§22.6).

  config.py            # Video-lane config: resolution/fps, TTS provider+voice,
                       #   music library path, Veo model ID, cadence policy,
                       #   opt-in flags. Editable without code changes.
```

### Responsibilities at a glance

| Module | Owns | Never does |
|--------|------|-----------|
| `orchestrator.py` | Stage sequencing, manifest, resumability, degradation | Block the daily text path; auto-post without approval |
| `script.py` | Narration + storyboard from insight | Invent numbers (claims come from grounded insight) |
| `motion_graphics.py` | **Every on-screen number/chart/label** | Delegate any digit to a generative model |
| `broll.py` | Text-free ambient Veo clips | Render text, faces, logos, or UI |
| `voiceover.py` | TTS audio + timing | Clone a voice without recorded consent |
| `music.py` | Licensed bed selection + loudness | Use unlicensed / AI-generated tracks (v1) |
| `captions.py` | Burned-in captions from our transcript | Depend on ASR guessing or platform-only captions |
| `assemble.py` | ffmpeg composite + mix + export | Alter factual content; skip caption burn-in |
| `upload.py` | Chunked resumable upload + attach | Reorder ETag parts; log tokens |

---

## 3. LinkedIn video upload flow (`/rest/videos`)

Videos are **not** the register-upload-attach dance used for images; they use a dedicated `/rest/videos` chunked flow. All calls carry `Linkedin-Version: <LI_VERSION>` and `X-Restli-Protocol-Version: 2.0.0`. Tokens are never logged (§22.10). Reference: LinkedIn Videos API (learn.microsoft.com/linkedin/marketing/community-management/shares/videos-api).

```
1) initializeUpload
   POST /rest/videos?action=initializeUpload
   body: { initializeUploadRequest: {
             owner:         "urn:li:person:… | urn:li:organization:…",
             fileSizeBytes: <int>,
             uploadCaptions:  false,      # captions are BURNED IN by us
             uploadThumbnail: <bool> } }
   ← returns:
       video URN            (the reel's URN, e.g. urn:li:video:…)
       uploadToken          (opaque, used at finalize)
       uploadInstructions[] (each: firstByte, lastByte, uploadUrl)

2) upload each part (parallel-safe, but keep ETag order)
   for instruction in uploadInstructions:
       bytes = file[firstByte : lastByte + 1]     # exact inclusive range
       resp  = PUT instruction.uploadUrl  body=bytes
       etag  = resp.headers["ETag"]               # collect, KEEP ORDER
   # On a mid-upload failure, only failed parts are re-PUT (resumable).

3) finalizeUpload
   POST /rest/videos?action=finalizeUpload
   body: { finalizeUploadRequest: {
             video:           <video URN>,
             uploadToken:     <uploadToken>,
             uploadedPartIds: [etag_0, etag_1, …] } }   # SAME ORDER as parts

4) poll processing status
   GET /rest/videos/{encoded video URN}
   until status == AVAILABLE            → proceed
        status == PROCESSING_FAILED     → fail the job (fail-closed)
   (backoff between polls; overall deadline enforced)

5) attach + post  (reuses publish/linkedin.py)
   POST /rest/posts
   body: { author, commentary, content: { media: { id: <video URN> } }, … }
```

**Upload guardrails**

- **Resumable state machine.** Persist `{video URN, uploadToken, part→ETag map, status}` so a crash/timeout resumes instead of restarting.
- **Idempotency & ordering.** `uploadedPartIds` must match `uploadInstructions` order exactly. Never reorder ETags.
- **Retry policy.** 429/5xx → exponential backoff; per-part retry, not whole-file.
- **Attach only when `AVAILABLE`.** Attaching a still-`PROCESSING` video risks a broken post → fail-closed if not available before deadline.
- **Never log tokens** (`uploadToken`, access token) or signed upload URLs.

---

## 4. Precision + authenticity guardrails (non-negotiable — BRD §23.3)

These are the difference between "world-class" and "AI slop." They are enforced **mechanically**, not assumed (§22.7).

1. **No fabricated numbers in generative video.** Every number, statistic, chart, and on-screen word is a **deterministic motion graphic** rendered by `motion_graphics.py`/`captions.py`. Generative models (Veo, diffusion) hallucinate text and digits → they are **text-free B-roll only**. Enforcement: `script.py` splits `narration` (spoken, may be generative TTS) from `on_screen_claims` (deterministic-only); a lint step rejects any claim that isn't grounded in the approved insight; `broll.py` runs a no-text heuristic on returned clips.
2. **Captions always.** Every reel ships with **burned-in** captions (accessibility, silent autoplay, comprehension). Never rely on the platform sidecar alone.
3. **Human approval, fail-closed.** Every reel is previewed in / linked from the approval email and **approved before posting** — same gate as text (§22.9). Any ambiguity → do not post.
4. **Opt-in / weekly, not daily.** Veo/voice renders take minutes and cost materially more than text. Video is **per-post opt-in or a weekly flagship** ("Insight of the Week"); the daily pipeline stays text/card with a "promote to reel" action. Cadence is config, not code.
5. **Authenticity & disclosure.** Google video carries a **SynthID** watermark. Default to **abstract text-free B-roll + the owner's real or cloned voice** — **never a synthetic face**. A "you" avatar (Level 4) needs dedicated avatar tooling, **explicit recorded consent**, and disclosure — a deliberate opt-in, not a default. Voice cloning (`voiceover.py`) is likewise consent-gated.
6. **Voice safety carries over (NFR-03).** No fabricated quotes attributed to real people; no clinical advice; healthcare claims framed appropriately.

---

## 5. Honest 2026 tooling reality (verify model IDs at build time)

| Capability | Tool (2026) | Status | Role in VISION |
|-----------|-------------|--------|----------------|
| **Direction / script** | **Claude** | Alive | Writer/director/orchestrator — narration, storyboard, assembly code. Renders no media. |
| **Video B-roll** | **Google Veo 3.1** (`veo-3.1-generate-preview`; 8s clips extendable to ~1 min; 720p–4K; 9:16) | **Alive — the video engine** | Text-free ambient B-roll only. SynthID watermarked. Via Brahmastra adapter. |
| **Unified / avatar** | **Gemini Omni** (announced May 2026, optional "looks/sounds like you" avatar) | Rolling out — **verify availability at build time** | Level 4 avatar only, consent-gated. |
| **Video (OpenAI)** | **Sora** | ⚠️ **App shut 26 Apr 2026; API scheduled to shut 24 Sep 2026** | **DO NOT build on Sora** — dead end. OpenAI's role shrinks to TTS. |
| **Voice-over / TTS** | **ElevenLabs** (best-in-class + consent-gated owner voice clone), OpenAI TTS, Google TTS | Alive, API-accessible | `voiceover.py` via Brahmastra adapter. |
| **Music** | Curated **licensed** library | Alive | `music.py`. No AI music in v1 (licensing clarity). |
| **Image gen** | Proven via **agy** (existing `visuals`/`brahmastra` image path) | Alive, already in-repo | Card lane + reel thumbnails/static frames. |

**Build-time rule:** model IDs (`veo-3.1-generate-preview`, TTS voice IDs, any Omni ID) are **config, not hard-coded**, and **must be verified live at build time** — the 2026 landscape moves fast (Sora is the cautionary tale). Do **not** commit a pipeline whose viability depends on an unverified or sunsetting model.

---

## 6. Phased plan (all post-core; each follows §22 conventions)

| Phase | Deliverable | Generative surface | Guardrail focus |
|-------|-------------|--------------------|-----------------|
| **5a — Carousel** | Deterministic branded multi-image swipe posts (lowest risk, high ROI) | None (reuses proven `visuals` image path via agy) | Precision by construction; no new AI risk |
| **5b — Motion-graphics reel + TTS** | 20–45s vertical Insight Reel: **deterministic motion graphics + TTS voice-over + burned-in captions**. **No generative video yet.** | TTS audio only | Numbers deterministic; captions always; human approval; opt-in/weekly |
| **5c — Veo B-roll + voice clone** | Add **text-free Veo 3.1 B-roll** accents + optional **ElevenLabs voice clone** (consent) | Veo video + cloned voice | No-text B-roll enforcement; SynthID disclosure; consent gate; verify Veo ID at build time |
| **5d** (context) | Analytics feedback loop + approval dashboard | — | — |
| **5e** (context) | Avatar — opt-in, disclosed, consent — only if desired | Avatar | Explicit consent + disclosure; deliberate opt-in |

**Recommended path:** build the core first, then **5a carousel → 5b Level-2 reels**, treating 5c (Level 3) and 5e (Level 4) as deliberate opt-ins. Each phase ships with green unit+integration tests and STAGING E2E before LIVE (§22.8).

---

## 7. Open decisions (not blocking v1)

- **Video ambition level:** none / Insight Reels (5b) / + Veo B-roll (5c) / + avatar (5e). *Recommendation: 5a → 5b, then treat 5c/5e as opt-ins.*
- **Voice:** owner voice clone (consent) vs. a chosen stock voice for the audio brand.
- **Cadence:** weekly flagship default vs. per-post "promote to reel" opt-in (both are config).
- **Music:** which licensed library / intro sting for audio-brand continuity.

---

## 8. Reality check + the three video paths (2026-07-08)

An end-to-end run and a Veo feasibility test settled the "real video" question:

- **Veo / true generative video — NOT available no-key.** `agy` (Antigravity)
  replied `VIDEO_NOT_AVAILABLE` ("not equipped with video generation tools or APIs
  like Veo in this environment"). Real motion video needs a **paid API key**, which
  the owner's no-key rule rules out. Do not build on Veo until that changes.
- **Motion-graphics reel (`src/vision/video/`) — the automated path.** Anime stills
  (agy) + edge-tts VO + ffmpeg Ken Burns + burned captions + open/close fade. Proven
  end-to-end (a real 16s reel). Fully automated, no key. This is "slideshow with
  motion + narration", not true video — honest naming.
  - **VO timing fix:** edge-tts omits `WordBoundary` for some voices, so duration
    came back 0; `voiceover.py` now probes the real mp3 duration via the bundled
    ffmpeg. Scenes now sync to the narration.
- **NotebookLM — the semi-manual flagship path.** No public API (only the Google
  Drive MCP is reachable), so it can't be automated. But `video/notebooklm_pack.py`
  produces a tight, on-message SOURCE PACK (+ a steering prompt) so NotebookLM's
  Video Overview stays <60s and presentable. Flow: VISION writes the pack to a
  Drive-synced folder -> owner generates + downloads the overview in NotebookLM ->
  VISION uploads + posts it (reuses `video/upload.py`).

**Recommendation:** motion-graphics for the automated daily/weekly reel; NotebookLM
for occasional polished flagship pieces; revisit Veo only if a keyless/subscription
path appears. Crossfade transitions between scenes are a further motion-graphics
polish (deferred — the xfade/audio-sync work needs care).

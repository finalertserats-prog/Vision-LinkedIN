# Brahmastra: the architecture of a self-hosted, multi-AI publishing engine

## THE ONE MESSAGE (the video must land this)
You can build a genuinely autonomous content engine that is safe *by construction* — three AIs deliberating, a human gate, and fail-closed guarantees at every step — without a single hosted SaaS in the loop.

## TARGET
A technically dense, confident explainer. First-person. Assume a builder audience — do not dumb it down. Show the ENGINEERING, not the vibes.

## STEERING PROMPT (paste into NotebookLM's customise box)
Make a sharp, technical video overview for a builder audience. Explain the ARCHITECTURE and the non-obvious engineering decisions below with confidence and precision. No hype, no "in this video", no basic definitions. Move fast, respect the viewer's intelligence.

## THE SYSTEM (source detail)

**What it is.** A self-hosted daily engine that ingests context, has three separate AI CLIs (Gemini/Antigravity, OpenAI Codex, Claude) genuinely *deliberate* over two rounds, composes a post in the owner's own voice, generates hand-drawn anime art with no API key, emails the owner for approval, and — only on a signed click — publishes to LinkedIn. Runs on the owner's machines and a Linux VPS. No hosted content SaaS anywhere.

**The multi-AI council (why three, not one).** A single model collapses to its own priors. Three models each give an independent round-1 take, then a round-2 where they *respond to each other* and can genuinely shift position. A composer then mines that transcript for the sharpest angle. The disagreement is the feature: it surfaces tension a single prompt would smooth away. An "honesty gate" refuses to manufacture a fight that did not happen.

**Fail-closed everywhere (the safety spine).**
- **Transactional outbox for publishing.** The approval state transition, single-use nonce consumption, and audit row COMMIT atomically *first*; only then is the publisher invoked. So a publish failure can neither lose the approval nor re-open the signed link. A background poller re-drives any un-published draft idempotently (dedup on the draft id + a "post_urn already set" no-op) — at-most-once at the edge, exactly-once overall, zero replay window.
- **Single-use HMAC approval tokens.** Each email action link carries a token keyed on sha256(draft_id | nonce), canonical-base64, action-scoped, expiring. An Approve link can never be replayed as a Post-now.
- **De-naming gate, fail-closed.** No AI/model name may reach published text OR text baked into an image. A leak aborts the compose rather than shipping. The #1 rule wins over shipping.

**The precision boundary (deterministic vs generative).** Diffusion and LLMs hallucinate digits and text, so they are *never* allowed near a number or an on-screen word. Every exact figure, caption, and label is rendered deterministically (Pillow / ffmpeg drawtext). Generative models are confined to TEXT-FREE atmosphere: anime concept art, contrast panels, TTS narration. Expressive AND precise, by construction, not by hope.

**Headless multi-CLI orchestration, no API keys.** The three models authenticate via cached OAuth token files, not pasted API keys — so the whole engine runs headless on a VPS with the CLIs already logged in. Image generation is done by driving the agent CLI itself ("save a PNG to this path"), which sidesteps the fact that no consumer image API is exposed. Voice-over uses a free public TTS endpoint. The entire generative surface costs nothing and needs no secret.

**Data lifecycle.** A weekly job archives rows/images older than a window to a compressed bundle plus a consistent SQLite snapshot, backs it up to Google Drive via rclone with a hash-verified check, and only THEN prunes locally and VACUUMs — never deletes anything it has not proven is safe off-box.

## THE ENGINEERING LESSONS WORTH SAYING OUT LOUD
- Autonomy is not "let it run" — it is a stack of fail-closed guarantees so that when it breaks (and it will), it breaks *safe*, never *loud*.
- The hard part of multi-agent systems is not making them talk; it is deciding what is deterministic and what is allowed to be generative, and enforcing that line mechanically.
- A human-in-the-loop that is only ever "approve/reject" is too coarse. The real seam is a signed, single-use, expiring action — cryptography doing the trust work, not vibes.

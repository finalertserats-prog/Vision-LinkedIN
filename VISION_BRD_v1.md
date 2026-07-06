# Business Requirements Document (BRD)
## Project VISION — Daily AI-Assisted Insight Engine for LinkedIn

> **Project name:** VISION — the daily insight engine.
> **One-line description:** A self-hosted pipeline that ingests fresh Life-Sciences/Healthcare and AI-tech signals daily, synthesises a thought-leadership post using the Brahmastra 3-AI ensemble, emails it to the owner for proof-reading and one-click approval, and — only on approval — publishes it to the owner's personal LinkedIn profile via the official LinkedIn API.

---

## 0. Document Control

| Field | Value |
|---|---|
| Document | Business Requirements Document |
| Project | VISION (Insight Engine) |
| Version | 1.3 (Draft for build) |
| v1.3 changes | Added §23 Expressive Media & Roadmap Suggestions (video, voice-over, carousels) per owner request — includes honest 2026 tooling reality (Veo alive, **Sora API shutting down 24 Sep 2026 — do not build on it**, Claude = director), a precision-first "richness ladder," authenticity guardrails, and suggested post-core phasing |
| v1.2 changes | Bound target repo to `finalertserats-prog/Vision-LinkedIN` (public, empty); bound synthesis dependency to `finalertserats-prog/God-Mode-Brahmastra` (private); added Brahmastra integration-via-introspection approach (§13.0); updated Phase 0 to init the repo + discover the real Brahmastra interface |
| v1.1 changes | Added explicit Autonomy Model (§1.1); promoted Visuals/Images to a first-class lane (§13.6, FR-21–23); added author identity + "powered by Brahmastra" signature config (§15.6, FR-24, D9); added image policy decision (D10) |
| Date | 2026-07-06 |
| Owner / Product Sponsor | Vishnu Dattu Kurnuthala |
| Primary consumer of this doc | Claude Code + Brahmastra (implementation agent) |
| Deployment target | Owner's VPS (Hostinger KVM, Ubuntu) and/or HP Victus server |
| Target repository | `github.com/finalertserats-prog/Vision-LinkedIN` (public, currently empty) |
| Synthesis dependency | `github.com/finalertserats-prog/God-Mode-Brahmastra` (private — Claude Code introspects it; see §13.0) |
| Status | Awaiting sign-off on Section 3 decisions, then build |

**How to use this document:** Sections 1–9 define *what* and *why* (business + requirements). Sections 10–19 define *how* (architecture, data, security, testing, deployment). Section 20 is the phased build plan with ready-to-paste RAFT prompts for each phase. Section 22 encodes the engineering conventions the implementation agent MUST follow. Appendices provide concrete config, feeds, and examples.

---

## 1. Executive Summary

The owner is an established healthcare operator (hospital owner) and technical builder who wants to publish a consistent stream of high-quality LinkedIn thought-leadership at the intersection of **Life Sciences / Healthcare best practices** and **global AI/technology developments** — framed around "what's happening, and how can we relate to and leverage it."

Producing this daily by hand is unsustainable. VISION automates *sourcing and drafting* while keeping the *human firmly in the loop*: nothing is ever posted without the owner reading it and clicking **Approve**. The synthesis is powered by the owner's existing Brahmastra multi-model system (Claude / Codex / Gemini), used as a **generate → critique → verify** chain so that accuracy and precision are enforced by the pipeline, not left to chance.

The build is deliberately **self-hosted on the owner's VPS** using the owner's existing stack (Python, PostgreSQL, cron, Docker) and the **official LinkedIn API** (self-serve `w_member_social` scope) — no third-party posting intermediary holds the content or tokens.

### 1.1 Autonomy Model (what is automated vs. what needs a human)

The system is **fully autonomous on the daily happy path, with exactly one intentional human gate: the owner's proof-read + Approve.** This is by design — the publish gate is *deliberately* not automated, because it is the owner's professional reputation on the line, and the owner has chosen to proof-read every post.

| Step | Automated? | Human touch |
|---|---|---|
| Source discovery, dedup, scoring, selection | ✅ Fully automated | — |
| Draft writing (generate → critique → verify) | ✅ Fully automated | — |
| Fact-grounding & quality gates | ✅ Fully automated | — |
| **Image decision + generation/rendering** | ✅ Fully automated | — |
| Approval email assembly + send | ✅ Fully automated | — |
| **Proof-read + Approve/Edit/Reject** | ❌ Human gate (intentional) | **Owner — ~2 min/day** |
| Publish to LinkedIn (text + image) | ✅ Fully automated on Approve | — |
| Confirmation, logging, retries, backups | ✅ Fully automated | — |

**Honest note on "fully autonomous":** the daily loop needs nothing beyond the Approve click. There is, however, minimal *periodic* upkeep, none of it daily: one-time setup (LinkedIn app, OAuth, feeds, deploy); a LinkedIn re-authorisation roughly **once a year** (refresh tokens last ~365 days); optional occasional feed/prompt tuning to keep quality high; and responding to an alert if something breaks. Everything auto-recovers where it safely can; where it can't (e.g. re-auth needed), the owner gets a clear alert.

---

## 2. Problem Statement, Objectives & Success Criteria

### 2.1 Problem Statement
Consistent, credible LinkedIn presence at the LS/HC × AI intersection requires (a) continuous monitoring of many sources, (b) synthesis into a distinctive voice, and (c) daily discipline. Doing all three manually does not scale. Fully automating it risks reputational damage (hallucinated facts, off-voice content, tone-deaf posts on sensitive healthcare topics).

### 2.2 Objectives
- **O1.** Automate daily discovery of relevant, fresh LS/HC and AI signals from reputable sources.
- **O2.** Synthesise one publish-ready LinkedIn post/day in the owner's voice, grounded in sourced facts.
- **O3.** Enforce a human proof-read + explicit approval before any publish. Never post without approval.
- **O4.** Publish approved posts to the owner's personal LinkedIn profile via the official API.
- **O5.** Guarantee accuracy/precision through mechanical fact-grounding and multi-model cross-checking.
- **O6.** Run reliably and observably on the owner's VPS with zero manual babysitting.

### 2.3 Success Criteria (measurable)
| ID | Criterion | Target |
|---|---|---|
| SC1 | Draft delivered to inbox on schedule | ≥ 98% of days, by target time |
| SC2 | Owner approval flow works end-to-end | 100% of approvals result in a correct post or a clear error |
| SC3 | Zero un-approved posts | 0 posts ever published without a valid, unexpired approval token |
| SC4 | Factual grounding | 100% of factual/numeric claims in a post trace to an ingested source; unsourced claims are removed or flagged |
| SC5 | No duplication | 0 posts semantically duplicating the owner's own posts from the last 90 days |
| SC6 | Deliverability | Approval emails land in inbox (not spam) ≥ 99% |
| SC7 | Recoverability | System auto-recovers from a dead feed, LLM outage, or LinkedIn 401/403/429 without manual intervention or double-posting |

---

## 3. KEY DECISIONS (require owner sign-off)

The BRD assumes the **Recommended** option in each row. Override any before build.

| # | Decision | Recommended (assumed) | Alternatives | Rationale for recommendation |
|---|---|---|---|---|
| D1 | LinkedIn publish method | **Official self-serve API (`w_member_social`)** | (a) Third-party wrapper (Ayrshare/Zernio/Postproxy); (b) Semi-manual "prefill composer"; (c) Browser automation | Full control, self-hosted, free, compliant, tokens never leave the VPS. See §7. |
| D2 | Approval mechanism | **Signed one-click links (Approve/Edit/Reject) in email + tiny edit page** | (a) Reply-to-approve email parsing; (b) Mini web dashboard; (c) Telegram/WhatsApp bot buttons | Lowest friction, secure (HMAC), works on mobile, supports light edits. |
| D3 | Cadence & content mix | **1 post/day, blended LS/HC × AI, rotating daily focus** | (a) Alternate pure-HC / pure-AI days; (b) 3×/week; (c) Multiple drafts to choose from | Consistency without fatigue; blended framing matches the owner's stated goal. |
| D4 | Email delivery | **Transactional provider (Resend/Postmark/Amazon SES)** | Owner's Hostinger SMTP with SPF/DKIM/DMARC | Approve-links must never hit spam; transactional providers maximise deliverability + give delivery webhooks. |
| D5 | Database | **PostgreSQL, dedicated `vision` schema** | SQLite (single-file, simplest) | Matches existing `fo_data` Postgres; concurrency-safe; easy backups. |
| D6 | Brahmastra invocation | **Abstraction supporting CLI subprocess AND direct model APIs; default = direct API inside the service** | CLI-only; single-model only | Direct API is more reliable/observable inside a long-running service; abstraction keeps the CLI path available. |
| D7 | Publish timing | **On approval, publish at next "optimal slot" (default 09:00 IST) with a "Post now" override in the email** | Immediate on approval only | Better reach; still fully owner-controlled. |
| D8 | Media | **First-class image lane: post gets a visual when it genuinely adds value (not every day)** | Text-only always | Owner wants images "at times"; a per-post decision step avoids forcing them. See §13.6. |
| D9 | "Powered by Brahmastra" | **Logo watermark on generated cards only (no text disclaimer on the post body)** | (a) Off entirely; (b) subtle text footer line in the post; (c) both | Branding without diluting authenticity; the post reads as authored by Vishnu. **Honest note:** a public "AI-drafted" text footer can, for some audiences, reduce perceived authenticity — since the owner writes the voice and approves every word, this is a personal-branding choice, not a requirement. Owner to pick a/b/c or default. |
| D10 | Image generation approach | **Split: deterministic render for anything with numbers/text (charts, stat/quote cards); diffusion model (gpt-image-1 / Imagen / Gemini) only for text-free concept illustrations, used sparingly** | (a) Diffusion for everything; (b) deterministic only | Precision (owner principle #4): diffusion models hallucinate numbers and render text poorly, so figures/words must be rendered deterministically. See §13.6. |

> **Sign-off line:** `Decisions approved as above ☐  /  Changes: ______________________`

---

## 4. Scope

### 4.1 In Scope
- Automated daily ingestion from curated RSS feeds + selected APIs (LS/HC and AI lanes).
- Deduplication, relevance scoring, recency filtering, and source selection.
- Brahmastra-powered generate → critique → verify synthesis into a LinkedIn-ready post.
- Optional per-post visual: deterministic branded cards/charts (precise) and sparing concept illustrations (image models), proof-read in the approval email.
- Fact-grounding and accuracy/precision quality gates.
- Daily approval email with sources, quality report, and signed action links.
- Optional light in-browser edit before approval.
- Publishing approved posts to the owner's **personal** LinkedIn profile via official API.
- Token lifecycle management (refresh before 60-day expiry).
- Observability, alerting, retries, idempotency, backups.
- Full automated test suite (unit + integration + E2E) and data-quality checks.

### 4.2 Out of Scope (v1)
- Posting to LinkedIn **company pages** (requires the reviewed `w_organization_social` / Marketing API tier).
- LinkedIn **articles, newsletters, polls, @mentions, document/PDF carousels** (not supported by the API — see §7.4).
- Multi-platform distribution (X, Threads, Medium) — future enhancement.
- Engagement automation (auto-commenting, auto-connecting) — explicitly excluded (ToS risk).
- Comment/reply reading and analytics beyond basic post-success confirmation (future enhancement).

---

## 5. Stakeholders & Personas

| Stakeholder | Role | Needs |
|---|---|---|
| Owner (Vishnu) | Author, approver, sole operator | High-quality drafts, frictionless approval, absolute control, zero embarrassing posts |
| Brahmastra (Claude/Codex/Gemini) | Synthesis engine | Clean, well-scoped prompts; grounded source material; deterministic output contracts |
| LinkedIn audience | Readers | Credible, non-clickbait, useful insights at LS/HC × AI intersection |
| Claude Code (build agent) | Implementer | This BRD, engineering conventions (§22), phase acceptance criteria |

**Primary persona (approver flow):** Owner reads the draft on mobile over morning coffee, spends < 2 minutes proof-reading, optionally taps *Edit* for a quick tweak, taps *Approve*. Done.

---

## 6. LinkedIn Publishing — Verified Facts (2026)

*(Grounded in current LinkedIn developer documentation and multiple 2026 API guides. These facts drive §7 and §15.)*

- **Endpoint:** `POST /rest/posts` (the Posts API, part of the Community Management API). It replaced the deprecated Shares/UGC APIs in 2024 and is the same endpoint for personal and company posting — only the **author URN** and **OAuth scope** differ.
- **Scope for personal posting:** `w_member_social` ("post, comment, like on behalf of an authenticated member"). This scope is available via the **self-serve** developer program (no enterprise Marketing-API review). To read the member id you also enable **Sign In with LinkedIn using OpenID Connect** (grants `openid`, `profile`, `email`).
- **Author URN:** `urn:li:person:{id}`, where `{id}` is the `sub` value returned by the OpenID Connect **userinfo** endpoint after login.
- **Auth:** OAuth 2.0, three-legged (3L) authorization-code flow. Access token lifetime ≈ **60 days**; refresh token lifetime ≈ **365 days**. Store both; refresh proactively.
- **Required header:** `LinkedIn-Version: YYYYMM` (e.g. `202506`) plus `X-Restli-Protocol-Version: 2.0.0` and `Authorization: Bearer <token>`.
- **Rate limit:** ~**100 calls/day/member** — far above the 1 post/day need.
- **App setup prerequisite:** the developer app must be **linked to a LinkedIn Company Page** and **verified**, even for personal-only posting. Create a placeholder page if needed.
- **Not supported by API (as of 2026):** native articles, newsletters, polls, @mentions in text, PDF/document carousels, and **editing a published post** (must delete + recreate). No native scheduling (build your own queue/cron).
- **Supported content:** text, images (upload via `/rest/images` first, reference the returned URN), video (`/rest/videos`, chunked), and link shares.

---

## 7. LinkedIn Method — Honest Options Analysis (Decision D1)

### 7.1 Option A — Official self-serve API `w_member_social`  ✅ RECOMMENDED
- **Pros:** Full control; self-hosted on VPS; free; compliant; tokens never leave your infrastructure; one-time OAuth for your own profile then silent refresh.
- **Cons:** One-time setup friction (dev app + placeholder company page + app verification + product enablement); must implement token refresh; API content limitations (§6).
- **Verdict:** Best fit for a world-class, self-owned, no-compromise build. **Chosen.**

### 7.2 Option B — Third-party unified posting API (Ayrshare, Zernio, Postproxy, etc.)
- **Pros:** Fastest MVP; wraps OAuth/token/scheduling; multi-platform later.
- **Cons:** Recurring cost; your drafted content + LinkedIn tokens flow through a third party; vendor lock-in/outage risk; some vendors use non-official access that can risk your account.
- **Verdict:** Documented **fallback** if Option A app-setup is blocked. Not default.

### 7.3 Option C — Semi-manual "prefill composer"
- **Pros:** Zero API/ToS risk; trivial to build.
- **Cons:** Not truly automated — on approval it only opens/prefills the LinkedIn composer for a final manual click/paste.
- **Verdict:** Safe **degraded mode** / emergency fallback if the API is ever unavailable.

### 7.4 Option D — Browser automation (Selenium/Playwright login)  ❌ REJECTED
- **Cons:** Violates LinkedIn ToS; high risk of the owner's real profile being restricted/banned; brittle against UI changes; stores LinkedIn password.
- **Verdict:** **Explicitly rejected.** The owner's profile is a professional asset; not worth the risk. Do not implement.

**Design decision:** Build Option A as primary, with Option C wired as a manual fallback behind a config flag. Option B remains a documented pivot.

---

## 8. Functional Requirements

| ID | Requirement | Priority |
|---|---|---|
| FR-01 | Ingest items from a configurable set of RSS feeds and APIs across two lanes (LS/HC, AI). | Must |
| FR-02 | Normalise each item to a common schema (title, url, source, published_at, summary, lane, raw). | Must |
| FR-03 | Deduplicate items (by URL + title similarity + content hash). | Must |
| FR-04 | Score items for relevance, recency, and source authority; select the top candidates. | Must |
| FR-05 | Generate a LinkedIn post draft grounded in selected items, in the owner's voice. | Must |
| FR-06 | Run a critique/editor pass (different model) to tighten, fix tone, enforce format. | Must |
| FR-07 | Run a verification pass that checks every factual/numeric claim against source material; remove/flag unsupported claims. | Must |
| FR-08 | Produce a quality report (length, hook presence, claim-grounding %, dedup check, tone/compliance flags, confidence score). | Must |
| FR-09 | Compose and send one daily approval email with the draft, sources/citations, and quality report. | Must |
| FR-10 | Provide signed, single-use, expiring **Approve / Reject / Edit** links. | Must |
| FR-11 | Provide an optional edit page to modify the post text before approving. | Should |
| FR-12 | On Approve, enqueue the post; publish via LinkedIn API at the configured slot (or immediately if "Post now"). | Must |
| FR-13 | Guarantee idempotency: an approved draft is published at most once. | Must |
| FR-14 | On publish, store the returned post URN and email the owner a confirmation with the live link. | Must |
| FR-15 | On Reject, discard (and optionally trigger one regeneration attempt). | Should |
| FR-16 | Auto-expire un-actioned drafts after a cutoff time (default 20:00 IST) — no post that day. | Must |
| FR-17 | Refresh LinkedIn tokens before expiry; alert owner if re-auth is ever required. | Must |
| FR-18 | Deduplicate against the owner's own posts from the last 90 days (semantic similarity). | Must |
| FR-19 | Expose a health/canary endpoint and emit structured logs + failure alerts. | Must |
| FR-20 | Support DRY_RUN (no email, no post), STAGING (email self, post-then-delete test), LIVE modes. | Must |
| FR-21 | Run an **image-decision** step per post: decide `none` \| `informative-card` \| `concept-illustration`, based on whether a visual adds value and what type fits. | Should |
| FR-22 | Render **informative visuals deterministically** (charts / on-brand stat/quote cards via HTML/SVG→PNG or matplotlib/Plotly) so every number and word is exact; **never** send figures/text through a diffusion model. | Should |
| FR-23 | Generate **concept illustrations** via a configurable image model (OpenAI `gpt-image-1` / Google Imagen / Gemini image) through a `BrahmastraImageClient`, using a fixed style guide, no embedded text; include any image **in the approval email** so the owner proof-reads it, and allow regenerate/swap/drop on the edit page. Upload approved images to LinkedIn via `/rest/images` and attach. | Should |
| FR-24 | Publish every post as authored by the owner (inherent to personal-profile posting); optionally apply the configured **Brahmastra signature** (logo watermark on cards and/or text footer) per D9. | Must (author) / Should (signature) |

---

## 9. Non-Functional Requirements

| ID | Category | Requirement |
|---|---|---|
| NFR-01 | **Accuracy** | Every factual/numeric claim in a published post MUST be traceable to an ingested source captured in the run record. The verifier removes or flags anything else. (Owner principle: *Accuracy is Key.*) |
| NFR-02 | **Precision** | Numbers, dates, entity names, and quotes are checked verbatim against sources; approximate/rounded figures must be labelled as such. (Owner principle: *Precision is Principle.*) |
| NFR-03 | **Safety of voice** | No fabricated quotes attributed to real people; no medical/clinical advice; healthcare claims carry appropriate framing; no defamatory or speculative claims about named organisations. |
| NFR-04 | **Human control** | Zero posts without a valid, unexpired approval action. Fail-closed. |
| NFR-05 | **Security** | Secrets encrypted at rest; action links HMAC-signed + single-use + expiring; all endpoints HTTPS; least-privilege OAuth scopes. |
| NFR-06 | **Privacy** | No third party receives LinkedIn tokens or unpublished drafts (Option A). Logs redact secrets. DPDP-mindful handling. |
| NFR-07 | **Reliability** | Retries with exponential backoff on transient failures; graceful degradation if a feed/model is down; no crash-looping. |
| NFR-08 | **Observability** | Structured logs, run records, health/canary endpoint, alerting to a channel the owner monitors. |
| NFR-09 | **Performance** | Full daily run (ingest → synthesise → email) completes in < 5 minutes under normal conditions. |
| NFR-10 | **Maintainability** | Modular code; per-module config; feeds/prompts editable without code changes; conventions in §22 followed. |
| NFR-11 | **Cost** | Predictable; only LLM API + optional email provider costs; documented per-run token budget. |
| NFR-12 | **Recoverability** | Postgres backed up nightly; a re-run of any stage is safe and idempotent. |

---

## 10. System Architecture

### 10.1 Components
```
                          ┌────────────────────────────────────────────┐
                          │                  VPS (Ubuntu)               │
                          │                                             │
  RSS / APIs ──► [1] INGEST ──► [2] CURATE (dedup+score+select)         │
                          │            │                                │
                          │            ▼                                │
                          │      [3] SYNTHESISE (Brahmastra)            │
                          │       generate → critique → verify          │
                          │            │                                │
                          │            ▼                                │
                          │      [4] QUALITY GATES ──► draft record ────┼──► PostgreSQL (vision schema)
                          │            │                                │
                          │            ▼                                │
                          │      [5] EMAIL COMPOSER ──► transactional ──┼──► Owner's finalert inbox
                          │                                provider     │        │
                          │                                             │        │ (clicks Approve/Edit/Reject)
                          │      [6] FastAPI approval service ◄─────────┼────────┘  (signed links, always-on)
                          │            │ (on Approve → enqueue)         │
                          │            ▼                                │
                          │      [7] PUBLISH WORKER ──► LinkedIn /rest/posts
                          │            │                                │
                          │            ▼                                │
                          │      [8] CONFIRM EMAIL + [9] OPS/OBSERVABILITY (health, logs, alerts, backups)
                          └────────────────────────────────────────────┘
```

### 10.2 Process model
- **`vision-web`** (always-on): FastAPI service exposing `/approve`, `/reject`, `/edit`, `/healthz`. Runs under systemd/Docker behind the existing reverse proxy (Traefik/nginx) with TLS.
- **`vision-daily`** (cron-triggered, e.g. 06:30 IST): runs Ingest → Curate → Synthesise → Quality → Email, writes a `draft` record, exits.
- **`vision-publisher`** (worker): either a small always-on consumer of an approved-queue, or a cron every 5 min that publishes any `approved && due` drafts. Recommend a lightweight poller for simplicity.
- **`vision-token`** (cron, daily): refreshes LinkedIn tokens if within the refresh window; alerts if re-auth needed.

### 10.3 Daily timeline (IST; configurable)
| Time | Stage |
|---|---|
| 06:30 | `vision-daily`: ingest + curate + synthesise + quality → store `draft` |
| 06:45 | Approval email sent to finalert inbox |
| ~morning | Owner proof-reads, optionally edits, clicks **Approve** (or **Post now**) |
| 09:00 (default) | `vision-publisher` publishes approved-and-due draft; confirmation email sent |
| 20:00 | Any un-actioned draft auto-expires (`expired`) — no post today |
| 02:00 | Nightly Postgres backup; token-refresh check |

### 10.4 Draft state machine
```
new ──► drafted ──► pending_approval ──► approved ──► queued ──► published
                          │                   │                     │
                          ├──► rejected        └──► publish_failed ──► (retry/backoff) ──► published | dead_letter
                          └──► expired
```
Rules: transitions are logged with actor + timestamp; only `pending_approval` may go to `approved`/`rejected`; a valid unexpired token is required for `approved`; `published` is terminal and idempotent (a second approve is a no-op).

---

## 11. Data Model (PostgreSQL — schema `vision`)

> Every table has `id UUID PK`, `created_at`, `updated_at`. Comments below are illustrative; the implementer must document each column in migrations per §22.

### 11.1 `sources`
| column | type | notes |
|---|---|---|
| name | text | e.g. "STAT News" |
| lane | text | `hc` \| `ai` |
| kind | text | `rss` \| `api` \| `scrape` |
| url | text | feed/endpoint |
| authority_weight | numeric | 0–1, source trust weight for scoring |
| enabled | bool | toggle without code change |
| last_ok_at | timestamptz | feed-health tracking |

### 11.2 `items` (ingested raw signals)
| column | type | notes |
|---|---|---|
| source_id | uuid FK | |
| lane | text | `hc` \| `ai` |
| title | text | |
| url | text | unique-ish |
| published_at | timestamptz | |
| summary | text | source-provided abstract/snippet |
| content_hash | text | for dedup |
| relevance_score | numeric | computed |
| selected | bool | chosen for a draft |
| run_id | uuid FK | which daily run captured it |

### 11.3 `runs` (one per daily execution)
| column | type | notes |
|---|---|---|
| status | text | `ok` \| `partial` \| `failed` |
| stats | jsonb | counts, timings, token usage, model versions |
| notes | text | |

### 11.4 `drafts`
| column | type | notes |
|---|---|---|
| run_id | uuid FK | |
| lane_focus | text | rotating focus of the day |
| post_text | text | final candidate |
| hashtags | text[] | |
| source_item_ids | uuid[] | provenance |
| quality_report | jsonb | see §14.4 |
| confidence | numeric | 0–1 |
| state | text | state machine (§10.4) |
| approve_token_hash | text | HMAC of the issued token (never store raw) |
| token_expires_at | timestamptz | |
| scheduled_for | timestamptz | publish slot |
| post_urn | text | LinkedIn URN after publish |
| post_url | text | live link |
| model_trace | jsonb | which model did generate/critique/verify + versions |

### 11.5 `own_posts` (dedup memory of the owner's published posts)
| column | type | notes |
|---|---|---|
| draft_id | uuid FK | |
| post_urn | text | |
| post_text | text | |
| embedding | vector | (pgvector) for semantic dedup |
| published_at | timestamptz | |

### 11.6 `oauth_tokens`
| column | type | notes |
|---|---|---|
| provider | text | `linkedin` |
| access_token_enc | bytea | encrypted |
| refresh_token_enc | bytea | encrypted |
| access_expires_at | timestamptz | |
| refresh_expires_at | timestamptz | |
| member_urn | text | `urn:li:person:{sub}` |

### 11.7 `audit_log`
Append-only: `(entity, entity_id, action, actor, ip, meta jsonb, at)`.

---

## 12. Content Sourcing Strategy

### 12.1 Principles
- **RSS/API-first.** Prefer feeds and official APIs over HTML scraping for reliability and ToS-cleanliness. Scrape only where a source has no feed AND permits it; respect `robots.txt`; identify with a proper User-Agent; rate-limit.
- **Two lanes, blended output.** Ingest HC and AI separately; the daily draft blends them around a rotating focus (see §13.2).
- **Recency window.** Default: items published in the last 48h (configurable), with a 7-day fallback if a lane is thin.
- **Authority weighting.** Each source carries a trust weight used in scoring.

### 12.2 Candidate source list (starter set — edit in `sources` table)
**Life Sciences / Healthcare lane:**
- STAT News, Endpoints News, FiercePharma / FierceHealthcare, Healthcare IT News, MobiHealthNews, Nature Medicine (feed), NEJM (feed), WHO news, PubMed New/Trending (E-utilities API), FDA press releases, McKinsey/Deloitte health insights (feeds), Rock Health / CB Insights digital-health reports.
**AI / Technology lane:**
- arXiv (cs.AI, cs.LG, cs.CL feeds), The Batch (deeplearning.ai), MIT Technology Review (AI), Anthropic / Google DeepMind / OpenAI / Microsoft Research blogs, Hugging Face blog, Hacker News (front-page API, filtered), Import AI newsletter, Papers with Code trends.
**Cross-cutting (LS/HC × AI):**
- Healthcare-AI specific: Nature Digital Medicine, JAMA AI, Health Affairs, STAT + AI tag, FDA AI/ML guidance.

> The implementer seeds these into `sources` with `authority_weight` and `enabled`. Owner curates over time.

### 12.3 Scoring (initial heuristic; tune later)
```
score = w_recency * recency(published_at)
      + w_authority * source.authority_weight
      + w_relevance * semantic_relevance(item, owner_topic_profile)
      + w_crosscut * bonus_if_bridges_HC_and_AI
```
`owner_topic_profile` = an editable list/embedding of the owner's themes (healthcare operations, RCM/claims, digital health, applied AI, data/BI, etc.). Cross-cutting items get a bonus because the owner's niche is the *intersection*.

### 12.4 Deduplication
Item-level: exact URL, then normalised-title fuzzy match, then content-hash. Cross-day: don't re-surface an item used in a draft in the last 14 days.

---

## 13. Synthesis Engine (Brahmastra) Design

### 13.0 Brahmastra integration — discover the real interface, do not assume
The synthesis brain is the owner's existing **God-Mode-Brahmastra** system (`github.com/finalertserats-prog/God-Mode-Brahmastra`, private). This BRD deliberately does **not** hard-code Brahmastra's call signature, because the interface must be taken from the source of truth, not guessed.

**Implementation instruction (Phase 0):** Claude Code (which is authenticated to the private repo) MUST first **read God-Mode-Brahmastra** and document its actual interface before writing any synthesis code. Capture: (a) invocation mode — CLI command(s), importable Python module, or HTTP endpoint; (b) whether a specific model can be targeted per call (needed to route generate/critique/verify across different models); (c) input format (prompt/messages, system prompt support); (d) output format (plain text vs structured JSON); (e) auth/keys, rate limits, timeouts, and error surface.

**Adapter pattern:** implement `BrahmastraClient` as a **thin adapter** over the discovered interface, exposing a stable internal contract the rest of VISION depends on:
```
BrahmastraClient.generate(prompt, model=None) -> {text|json}
BrahmastraClient.critique(prompt, model=None) -> {text|json}
BrahmastraClient.verify(prompt, model=None)  -> {text|json}
BrahmastraImageClient.illustrate(prompt, model=None) -> image_bytes   # §13.6 concept illustrations
```
If Brahmastra exposes per-model routing, map `MODEL_GENERATE / MODEL_CRITIQUE / MODEL_VERIFY` (Appendix A) onto it so the three passes use different models for genuine cross-checking. If it does not, fall back to a single model with distinct RAFT prompts per pass and record that limitation in the run's `model_trace`. Keep VISION coupled only to the adapter, never to Brahmastra's internals, so a Brahmastra change is a one-file update.

### 13.1 Pipeline: generate → critique → verify
Three passes, ideally across **different models** in the Brahmastra ensemble to reduce single-model bias/hallucination:
1. **Generate** (Model A): draft the post from selected items + owner voice profile.
2. **Critique/Edit** (Model B): tighten hook, fix tone, enforce LinkedIn format, cut fluff, check the owner-voice, return an improved version + a change log.
3. **Verify** (Model C or A with a strict checker prompt): extract every factual/numeric/named claim, map each to a source item, and REMOVE or FLAG any claim not supported. Output a claim-grounding table.

### 13.2 Rotating daily focus (D3)
A 7-slot rotation to keep variety, e.g.:
`Mon: AI in clinical ops | Tue: RCM/claims + automation | Wed: pharma/biotech tech | Thu: data/BI in healthcare | Fri: frontier AI + "how HC can leverage" | Sat: patient experience + tech | Sun: leadership/best-practice reflection`
Each day, the blended draft is anchored to that focus but may weave in the day's strongest cross-cutting signal.

### 13.3 Owner voice profile (editable config)
A short spec the generator always receives: tone (credible, pragmatic, operator's-eye-view, non-hype), perspective (hospital owner + technical builder), do's (concrete takeaways, "so what for practitioners"), don'ts (no clickbait, no fake urgency, no emoji-spam, no fabricated stats, no medical advice), structure (hook line → 3–5 short paragraphs → one actionable takeaway → 3–5 hashtags), length (target 1,100–1,900 characters).

### 13.4 Prompt contracts (RAFT-structured — see §22)
Each pass uses a **Role–Action–Format–Target** prompt with a strict output schema (JSON) so parsing is deterministic. Example contract for the Generate pass:
- **Role:** "You are a healthcare-technology thought-leadership ghostwriter writing in the first person as [owner voice profile]."
- **Action:** "Draft one LinkedIn post that blends today's selected HC and AI signals around the focus '{focus}'. Ground every claim in the provided sources. No unsupported facts."
- **Format:** JSON `{ "hook": ..., "body": ..., "takeaway": ..., "hashtags": [...], "claims": [{ "text":..., "source_item_id":... }] }`.
- **Target:** "Audience: LinkedIn — healthcare leaders, clinicians, health-tech builders. Voice: pragmatic operator, no hype."

Verify pass returns `{ "grounded": [...], "unsupported": [...], "revised_post": ..., "confidence": 0-1 }`.

### 13.5 Accuracy/precision controls (NFR-01/02)
- Claims table with per-claim source mapping; grounding % is computed and gated (e.g. must be 100% for auto-eligibility; < 100% → flagged prominently in the email).
- Numeric/entity claims get a stricter verbatim check.
- Any statistic must include its source and time reference; rounded numbers labelled "~".
- Confidence score surfaced to the owner; low confidence is highlighted, never hidden.

### 13.6 Visual / Image lane (D8, D10 — precision-first)

Images are a **first-class, optional** part of each post — used when they add value, not every day.

**Step 1 — Image decision (per post).** After the post text is finalised, a decision step returns one of:
- `none` — text-only (default when no visual clearly helps).
- `informative-card` — the post centres on a concrete stat, comparison, or simple process → render a visual **deterministically**.
- `concept-illustration` — the post is conceptual and benefits from a tasteful abstract visual → generate via image model.

**Step 2a — Deterministic renderer (informative-card / charts).** Anything containing **numbers or words** is rendered by code, not by a diffusion model:
- On-brand card template (HTML/SVG → PNG via headless Chromium) or matplotlib/Plotly for charts, using the owner's palette (navy/gold, aligned to the BRAHMASTRA brand).
- Guarantees: figures are exactly the sourced numbers (precision), text is crisp, layout is consistent and branded. Optional discreet **BRAHMASTRA logo watermark** here (D9).
- Every number on a card must trace to a grounded claim in the post's source set (reuses the §13.5 grounding gate).

**Step 2b — Diffusion image client (concept-illustration).** For **text-free** abstract visuals only:
- `BrahmastraImageClient` routes to a configurable model: OpenAI `gpt-image-1` (Images API), Google Imagen, or Gemini image generation. *(Confirm exact model IDs at build time — image model names change; keep them in config, mirroring `MODEL_GENERATE` etc.)*
- A fixed **style guide** (e.g. "minimal, professional, muted palette, no text, no logos, editorial") keeps outputs consistent and avoids "AI-slop."
- **No embedded text or numbers** — if words are needed, use a deterministic card instead.
- Used sparingly (concept illustrations are the minority case).

**Step 3 — Human proof-read of the image.** The chosen image is embedded in the approval email and shown on the edit page; the owner can **approve / regenerate / swap type / drop** it. Nothing visual is posted without the owner seeing it.

**Step 4 — Publish.** On approval, an image is uploaded to LinkedIn via `/rest/images` (get URN) and attached to the post payload (§15.2). v1 = single image; multi-image carousels are a later enhancement.

**Specs / guardrails.** Target LinkedIn feed dimensions (≈1200×627 landscape or 1200×1200 square); size/format validated before upload; a per-week image cap is configurable so cadence stays natural; image generation failures degrade gracefully to a text-only post (never block publishing).

**Data model additions (extend `drafts`):** `image_type` (`none|informative-card|concept-illustration`), `image_path`, `image_source` (`deterministic|<model-id>`), `image_prompt`, `image_urn` (after LinkedIn upload).

---

## 14. Approval Workflow

### 14.1 Email contents
- Subject: `VISION daily draft — {focus} — {date}`
- The proposed post exactly as it would appear (char count shown).
- **Quality report** (grounding %, dedup result, tone/compliance flags, confidence).
- **Sources** used (titles + links) so the owner can spot-check.
- Buttons: **Approve & schedule (09:00)** · **Post now** · **Edit** · **Reject**.
- Footer: run id, expiry time.

### 14.2 Signed action links (NFR-05)
- Each link carries a token = `base64url(draft_id | action | exp | nonce)` + HMAC-SHA256 signature using a server secret.
- **Single-use** (nonce checked against `used_tokens`), **expiring** (default 20:00 IST same day), **action-scoped**.
- Endpoint verifies signature + expiry + single-use before any state change. Invalid/expired → friendly "link no longer valid" page.
- Because these links can act on the owner's LinkedIn, treat them like magic-login links: HTTPS only, no secrets in query beyond the signed token, rate-limited.

### 14.3 Edit page (FR-11)
Minimal HTML page (served by `vision-web`) pre-filled with the draft; owner edits text/hashtags, sees live char count, clicks **Approve edited**. The edited text replaces `post_text`; re-runs the length/format/compliance checks (not the full LLM) before allowing approve.

### 14.4 `quality_report` shape (jsonb)
```json
{
  "char_count": 1523,
  "has_hook": true,
  "grounding_pct": 100,
  "unsupported_claims": [],
  "dedup_vs_own_90d": { "max_similarity": 0.31, "pass": true },
  "tone_flags": [],
  "compliance_flags": [],
  "hashtags": ["#HealthTech", "#AIinHealthcare", "#DigitalHealth"],
  "confidence": 0.86
}
```

### 14.5 Fail-safes
- Un-actioned by cutoff → `expired`, no post (FR-16).
- Reject → discard; optional single regeneration (config).
- Approve on an already-`published` draft → no-op (idempotent).
- If publish fails after approve → retry with backoff; after N tries → `dead_letter` + alert; never silently drop.

---

## 15. LinkedIn Publishing (Option A implementation)

### 15.1 One-time setup (owner, documented as a runbook)
1. Create a LinkedIn **Developer App**; link it to a Company Page (create a placeholder if needed); **verify** the app.
2. Enable products: **Sign In with LinkedIn using OpenID Connect** and **Share on LinkedIn**.
3. Configure OAuth redirect URL to `https://<vps-domain>/oauth/linkedin/callback`.
4. Run a one-time `authorize` command → owner logs in → app stores access + refresh tokens (encrypted) and `member_urn` (`urn:li:person:{sub}` from userinfo).

### 15.2 Publish call (per approved draft)
- `POST https://api.linkedin.com/rest/posts`
- Headers: `Authorization: Bearer <access>`, `LinkedIn-Version: <YYYYMM>`, `X-Restli-Protocol-Version: 2.0.0`, `Content-Type: application/json`.
- Body (text post): author = `member_urn`, `commentary` = post text, `visibility` = `PUBLIC`, `lifecycleState` = `PUBLISHED`, distribution defaults.
- Response → capture the created post **URN**; build the live URL; store on the draft; email confirmation.
- **Image post:** when the approved draft has an image, first register + upload via `/rest/images` (upload bytes, get the image URN), then reference that URN in the `/rest/posts` payload's content. Text-only otherwise.

### 15.3 Token lifecycle
- `vision-token` checks daily; if access token within e.g. 7 days of expiry, refresh using the refresh token.
- If refresh token near expiry or refresh fails → alert owner to re-authorize (one login).
- All tokens encrypted at rest; never logged.

### 15.4 Error handling
| Case | Handling |
|---|---|
| 401 Unauthorized | Attempt refresh; if still 401 → alert to re-auth; do not lose the approved draft |
| 403 Forbidden | Log scope/role issue; alert; likely misconfigured product/scope |
| 429 Rate limited | Backoff + retry later (we're far under limits, so this is defensive) |
| 5xx | Exponential backoff, capped retries, then `dead_letter` + alert |
| Duplicate publish guard | Idempotency key = draft_id; if `post_urn` already set, no-op |

### 15.5 Degraded mode (Option C fallback)
Config flag `PUBLISH_MODE = api | prefill`. In `prefill`, on approval the system emails/opens a LinkedIn share-composer URL pre-filled with the text for a final manual click — used only if the API is unavailable.

### 15.6 Author identity & "Powered by Brahmastra" (D9)
- **Author = Vishnu, automatically.** Because we post to the owner's personal profile with `w_member_social`, the author is inherently the owner's member account (`urn:li:person:{sub}`). Every post appears as authored by **Vishnu Dattu Kurnuthala** — no configuration or extra work required, and no way (or need) to set a different author.
- **Brahmastra signature (configurable via `POST_SIGNATURE_MODE`):**
  - `off` — no signature; cleanest, reads as pure owner content.
  - `card_watermark` (**recommended, D9 default**) — discreet BRAHMASTRA logo/wordmark stamped only on **deterministically-rendered cards** (not on the post text). Branding without a disclaimer on the writing.
  - `text_footer` — appends a configurable last line to the post body, e.g. `POST_SIGNATURE_TEXT="— curated via Brahmastra, my multi-AI system"`.
  - `both` — watermark on cards **and** text footer.
- The signature choice is purely presentational and never affects the author identity or the grounding/quality gates.

---

## 16. Security & Privacy

- **Secrets:** `.env` not committed; encryption key for tokens stored in a secrets store or OS keyring; consider `sops`/`age` or a KMS. Rotate.
- **Action links:** HMAC-signed, single-use, expiring, action-scoped, HTTPS-only, rate-limited (§14.2).
- **Transport:** all endpoints behind TLS via the existing reverse proxy.
- **Least privilege:** request only `w_member_social`, `openid`, `profile`, `email`.
- **Data minimisation & DPDP-mindfulness:** store only what's needed; redact secrets in logs; no third party receives tokens/drafts (Option A).
- **Auditability:** append-only `audit_log` for every state change and publish.
- **Abuse resistance:** the approval endpoints are the only externally reachable surface — keep them tiny, validated, and monitored.

---

## 17. Observability & Operations

- **Structured logging** (JSON) per stage with `run_id` correlation.
- **`/healthz`** returns pipeline + DB + token status; a **canary** (reuse the FinalAlert pattern) pings it and alerts on failure.
- **Alerts** to a channel the owner watches (email/Telegram): daily-run failure, publish failure, token-refresh-needed, dead feed, dead_letter.
- **Feed health:** track `sources.last_ok_at`; alert if a source has been silent > threshold.
- **Backups:** nightly `pg_dump` of the `vision` schema; retain 14 days; test restore.
- **Runbook:** documented procedures for re-auth, backfill, replay a failed publish, disable a bad feed.

---

## 18. Testing Strategy (owner principle: end-to-end + data/code quality)

### 18.1 Levels
- **Unit:** feed parsers, normaliser, dedup, scorer, token signer/verifier, prompt-output parsers, LinkedIn client (mocked HTTP), state machine transitions.
- **Integration:** ingest→curate→synthesise on fixture feeds; email compose+send to a test inbox; approval endpoints against a temp DB.
- **End-to-end:** full pipeline in **STAGING** mode — generates a real draft, emails the owner, on approval **posts then immediately deletes** a clearly-marked test post (LinkedIn has no draft state), verifying the whole loop against the live API safely.
- **Contract tests:** assert each LLM pass returns schema-valid JSON; fail loudly on drift.

### 18.2 Data-quality checks (gates before send/publish)
- Schema validation (pydantic) on every ingested item and every LLM output.
- Post assertions: char length in range, hook present, ≤ N hashtags, grounding % meets threshold, dedup pass, no banned phrases, no unresolved template tokens.
- Feed-freshness assertions; empty-lane fallback path tested.

### 18.3 Failure injection
Simulate: dead feed, LLM timeout/invalid JSON, LinkedIn 401/403/429/5xx, expired token, duplicate approve, expired token click. Assert graceful, no-double-post behaviour.

### 18.4 Acceptance criteria
Each phase (§20) has explicit acceptance criteria; a phase is "done" only when its tests are green and criteria met.

---

## 19. Deployment (VPS)

- **Containers (Docker Compose):**
  - `vision-web` (FastAPI, always-on) behind Traefik/nginx with TLS.
  - `vision-daily` (cron-invoked job container, or host cron running the module).
  - `vision-publisher` (poller) and `vision-token` (daily) as scheduled jobs.
  - Reuse the existing PostgreSQL instance (new `vision` schema) or a dedicated container.
- **Config:** all via env vars (§Appendix A); feeds/prompts/voice-profile in editable files or DB, not hard-coded.
- **Scheduling:** host `cron` or `systemd timers` for the daily/publisher/token jobs; document the crontab.
- **Resource guardrails:** memory limits on containers (mind the prior FinalAlert memory-overload incident); a watchdog/restart policy.
- **Secrets:** injected at runtime, never baked into images.
- **CI (optional):** GitHub Actions running the test suite on push to the repo (owner's `finalertserats-prog` org).

---

## 20. Phased Delivery Plan

> Follows the owner's conventions: **plan first, build in phases, one file at a time with explicit "go ahead" between files, fully-commented code, RAFT prompts, end-to-end tests.** Each phase ends with green tests + acceptance criteria before the next begins.

### Phase 0 — Foundations & De-risking (LinkedIn auth spike FIRST)
**Goal:** Prove the riskiest externals work before building anything else, and stand up the repo.
- **Repo init:** initialise the empty `Vision-LinkedIN` repo — README, LICENSE, `.gitignore`, package layout, `main` branch protection, and a CI workflow (GitHub Actions) that runs the test suite on push (owner's `finalertserats-prog` org).
- **Brahmastra introspection (§13.0):** read `God-Mode-Brahmastra`, document its real interface, and implement the `BrahmastraClient` adapter against it.
- Env config, Postgres `vision` schema + migrations, secrets handling.
- **LinkedIn spike:** create dev app + placeholder company page + verify + enable products + OAuth once + publish a single "hello world" test post via `/rest/posts`, then delete it.
- **Brahmastra spike:** via the adapter, confirm generate/critique/verify return schema-valid JSON (and, if supported, that per-pass model routing works).
- **Email spike:** send a test email with a working signed link that flips a DB flag.
**Acceptance:** repo initialised with green CI; the Brahmastra interface is documented and the adapter returns valid JSON; a test post appears+deletes on the owner's profile; a signed link toggles state.
**RAFT build prompt (paste to Claude Code):**
> **Role:** Senior Python platform engineer. **Action:** Initialise `finalertserats-prog/Vision-LinkedIN` for Project VISION: repo scaffolding + CI; then READ the private `finalertserats-prog/God-Mode-Brahmastra` repo, document its actual interface, and implement a `BrahmastraClient` adapter (BRD §13.0) — do NOT assume the interface. Then build: `.env` schema, PostgreSQL `vision` migrations for the tables in BRD §11, a `LinkedInClient` with `authorize()` + `publish_text()` + `publish_with_image()` + `delete()` using the official `/rest/posts` + `/rest/images` APIs and `w_member_social` (BRD §6/§15), and a signed-token module (BRD §14.2). Deliver ONE file at a time; present a plan and wait for "go ahead" before each file; fully comment every function/block. **Format:** modular Python package + Alembic migrations + runnable `spike_brahmastra.py`, `spike_linkedin.py`, `spike_email.py`. **Target:** VPS deployment; author is the sole operator (Vishnu).

### Phase 1 — Ingest & Synthesis (offline, no email/post)
**Goal:** Produce an excellent draft to console/file. Nail content quality.
- RSS/API ingestors, normaliser, dedup, scorer, source seeding.
- Brahmastra generate→critique→verify chain + claims-grounding + quality report.
- Own-post dedup memory (pgvector) scaffolding.
- **Image lane (§13.6):** image-decision step + the **deterministic card/chart renderer** (on-brand, watermark-capable). Diffusion `BrahmastraImageClient` can be stubbed here and finished when convenient.
**Acceptance:** given fixture + live feeds, produces a grounded, well-formatted draft with a quality report; grounding gate works; a stat/chart card renders with exact sourced numbers and correct branding; unit+integration tests green.

### Phase 2 — Approval Loop (email + endpoints; publishing mocked)
**Goal:** The human-in-the-loop works end-to-end with publishing stubbed.
- Email composer + transactional send; signed Approve/Reject/Edit links; FastAPI endpoints; edit page; state machine; expiry job.
**Acceptance:** owner receives email, can approve/edit/reject; state transitions correct; expired/duplicate/invalid links handled; publishing mock called exactly once on approve.

### Phase 3 — LinkedIn Publishing (real)
**Goal:** Approved drafts actually post.
- Wire real `LinkedInClient.publish`; **image upload via `/rest/images` + attach**; token refresh job; confirmation email; idempotency; retries/backoff; DRY_RUN→STAGING→LIVE.
- Finalise the diffusion `BrahmastraImageClient` (concept illustrations) + style guide, and the `POST_SIGNATURE_MODE` handling (§15.6).
**Acceptance:** STAGING E2E (post-then-delete) passes for both text-only and image posts; LIVE publishes a real approved post (with image when chosen) with correct URN + confirmation; image failure degrades gracefully to text-only; error matrix (§15.4) handled.

### Phase 4 — Ops, Observability & Hardening
**Goal:** Runs unattended, safely.
- Structured logging, `/healthz` + canary, alerting, feed-health, backups, security review (mirror the owner's privacy-audit habit), memory guardrails.
**Acceptance:** failure-injection suite passes; alerts fire correctly; backup+restore verified; no un-approved-post path exists.

### Phase 5 — Enhancements (optional, prioritised later)
Optimal-time scheduling refinements; **multi-image carousels**; a small library of reusable branded card templates; engagement analytics pull-back; multi-draft "pick one"; weekly digest; multi-platform distribution; A/B hooks. *(Single image per post is now core — see §13.6/Phase 1 & 3.)* **See §23 for the fuller expressive-media roadmap — video (Insight Reels), voice-over, carousels — with tooling reality and guardrails.**

---

## 21. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| LinkedIn app review / product enablement friction | Med | Med | Do the auth spike in Phase 0; placeholder company page; Option C fallback; Option B pivot documented |
| Hallucinated/incorrect facts published | Med | High | Multi-model verify pass + grounding gate + human approval + sources shown in email |
| Approval link abused | Low | High | HMAC + single-use + expiry + HTTPS + rate-limit + audit log |
| Token expiry breaks publishing | Med | Med | Proactive refresh job + re-auth alert; approved drafts never lost |
| Email lands in spam → missed approvals | Med | Med | Transactional provider + SPF/DKIM/DMARC; delivery webhooks; fallback channel |
| Repetitive/off-voice content | Med | Med | 90-day dedup + rotating focus + voice profile + critique pass |
| VPS memory overload (prior incident) | Low | Med | Container memory limits, watchdog, bounded batch sizes |
| Sensitive healthcare content misstep | Low | High | Compliance flags, no clinical advice, no fabricated quotes, human approval |
| Third-party dependency change (LinkedIn API) | Med | Med | Version header pinned; monitor changelog; abstraction isolates the client |
| Brahmastra interface unknown to this BRD (private repo) | Med | Low | Phase 0 introspects `God-Mode-Brahmastra`; `BrahmastraClient` adapter isolates VISION from its internals; single-file update if it changes |

---

## 22. Engineering Conventions (implementation agent MUST follow)

1. **Plan first.** Before writing code for any phase/module, present a plan of action and wait for explicit "go ahead."
2. **One file at a time.** When a task spans multiple files, deliver one file, then ask for explicit "go ahead" before the next.
3. **Fully-commented code.** Every function and non-trivial block has comments explaining the logic, so the code is understandable by reading comments alone.
4. **RAFT prompts.** All LLM prompts follow Role–Action–Format–Target; assign an explicit role (or ask which role) per prompt.
5. **Deterministic LLM contracts.** LLM passes return strict JSON validated against a schema; fail loudly on drift.
6. **Config over code.** Feeds, prompts, voice profile, schedules, thresholds are editable without code changes.
7. **Quality bar.** World-class, no shortcuts; accuracy and precision are enforced mechanically (grounding gate, verify pass), not assumed.
8. **Tests are part of "done."** No phase is complete without green unit+integration tests and its acceptance criteria met; STAGING E2E before LIVE.
9. **Fail-closed.** Any ambiguity in approval/publish resolves to "do not post."
10. **Secrets discipline.** No secrets in code, logs, or images; tokens encrypted at rest.

---

## 23. Expressive Media & Roadmap Suggestions (for consideration — not committed in v1)

Added per owner request: richer, more expressive, more intuitive formats (video, voice-over, carousels), with an **honest** read on the 2026 tooling and the quality/authenticity guardrails that keep them world-class rather than gimmicky. Nothing here is committed for v1; it is a menu to weigh during the build.

### 23.1 Can we make videos with Gemini / ChatGPT / Claude + voice-over? (honest tooling reality, 2026)
- **Claude** — does **not** render video, images, or audio. Its role is **director / writer / orchestrator**: narration script, storyboard, shot list, captions, and the code that assembles clips + voice-over + captions. This is the ideal division of labour, not a shortfall.
- **Gemini (Google)** — **yes, this is the video engine.** Veo 3.1 generates video with **native audio** (dialogue/SFX/music) via the Gemini API / Vertex AI (`veo-3.1-generate-preview`; 8s clips, extendable to ~1 min via scene-extension; 720p–4K; 16:9 or 9:16). Newer **Gemini Omni** (announced May 2026) adds unified multimodal generation and optional **AI avatars** ("looks and sounds like you"); its API was rolling out "in the following weeks" as of announcement — **verify current availability at build time**. All Google video carries a **SynthID watermark**.
- **ChatGPT / OpenAI (Sora)** — ⚠️ **Do NOT build on Sora.** The Sora app shut down (26 Apr 2026) and the **Sora API is scheduled to shut down 24 Sep 2026**. Any pipeline built on it is a dead end. OpenAI's useful role here shrinks to **TTS voice-over** and scripting.
- **Voice-over / TTS** — mature and API-accessible: **ElevenLabs** (best-in-class TTS + optional **voice clone of the owner, with consent** — authentic and scalable), OpenAI TTS, or Google TTS. Route through the Brahmastra adapter like the other models.
- **Publishing** — LinkedIn's API **supports video posts** (upload via `/rest/videos`, chunked with ETag tracking, then attach the URN), so approved reels can auto-publish through the same approval → publish flow.

### 23.2 The "richness ladder" — expressive AND credible (precision-first)
Pick a level per post; default stays low-cost, escalate only when it adds value:

| Level | Format | Engine | When |
|---|---|---|---|
| 0 | Text-only | — | Default many days |
| 1 | Branded static card | Deterministic render (§13.6) | A stat/quote worth a visual |
| 2 | **Insight Reel — recommended sweet spot** | Deterministic **motion graphics** (exact numbers animated, captions burned in) + **voice-over** (TTS / cloned voice) + music | The day's flagship insight; weekly at minimum |
| 3 | AI B-roll accent | Veo 3.1 clips (**text-free**, abstract/cinematic) *behind* the motion graphics | Sparingly, for atmosphere only |
| 4 | Talking avatar (opt-in) | HeyGen / Synthesia / Gemini Omni avatar, or a real self-recorded clip | High expressiveness, **highest authenticity risk — default OFF** |

**Sweet spot = Level 2:** a 20–45s vertical/square Insight Reel where the *factual content is rendered deterministically* (every number exact, every word crisp) and Veo/voice add motion and warmth. Expressive without betraying precision.

### 23.3 Non-negotiable guardrails (so video stays world-class, not slop)
- **Precision (principle #4):** numbers, text, and charts are **deterministic motion graphics — never** placed inside Veo/diffusion output (these hallucinate text/figures). Generative video is for **text-free B-roll only**.
- **Authenticity & disclosure:** generative video is watermarked (SynthID); a synthetic on-screen "you" is a reputational decision for a healthcare leader. Default to **abstract B-roll + the owner's real or cloned voice**, not a fake face. General video models restrict real-person likeness, so a "you" avatar needs dedicated avatar tooling **with explicit consent**, and should be disclosed.
- **Muted-first:** LinkedIn autoplays without sound → **burn in captions** always.
- **Human-in-the-loop:** every reel is previewed in / linked from the approval email and **approved before posting** — same fail-closed gate as text.
- **Cost & latency:** Veo/avatar/voice renders take minutes and cost materially more than text → video is **opt-in per post or a weekly flagship**, never forced daily. Daily pipeline stays text/card with a "promote to reel" action.
- **Accessibility:** auto-generate **captions + alt-text/transcript** (ethical, on-brand for healthcare, and boosts reach).

### 23.4 Other expressive / intuitive ideas worth weighing
- **Native multi-image carousel** (swipe posts): high engagement, API-supported (multi-image), deterministic branded slides — **lower-risk, high-impact; strong candidate before video.**
- **Weekly flagship cadence:** one deeper reel or carousel per week ("Insight of the Week"); daily stays light.
- **Engagement feedback loop:** pull post analytics → learn which hooks/formats perform → tune the generator. Closes the quality loop (principles #3, #5).
- **A/B hooks:** generate two hook variants; owner picks in the approval email.
- **Approval dashboard (beyond email):** a small web UI to preview reels, choose among variants, and view the analytics loop.
- **Audio brand:** a consistent short intro sting + one chosen voice for continuity.

### 23.5 Suggested phasing (all post-core; each follows §22 conventions)
- **Phase 5a — Carousel** (lowest risk, high ROI): deterministic branded multi-image swipe posts.
- **Phase 5b — Insight Reel v1:** motion graphics + TTS voice-over + burned-in captions (no generative video yet).
- **Phase 5c — Veo B-roll + voice clone:** add text-free Veo accents and optional ElevenLabs voice clone (with consent).
- **Phase 5d — Analytics feedback loop + approval dashboard.**
- **Phase 5e — Avatar (opt-in, disclosed):** only if desired.

> **Optional decision for later (not blocking v1):** *Video ambition level* — none / Insight Reels (Level 2) / + Veo B-roll (Level 3) / + avatar (Level 4). Recommendation: build the core first, then **carousel → Level 2 reels**, and treat Levels 3–4 as deliberate opt-ins.

---

## Appendix A — Environment Variables (starter)
```
# Core
VISION_ENV=live|staging|dry_run
TZ=Asia/Kolkata
DATABASE_URL=postgresql://.../vision
SECRET_HMAC_KEY=...
TOKEN_ENC_KEY=...

# LinkedIn
LI_CLIENT_ID=...
LI_CLIENT_SECRET=...
LI_REDIRECT_URI=https://<vps-domain>/oauth/linkedin/callback
LI_VERSION=YYYYMM
PUBLISH_MODE=api|prefill
PUBLISH_SLOT_LOCAL=09:00
APPROVE_CUTOFF_LOCAL=20:00

# Email
EMAIL_PROVIDER=resend|postmark|ses|smtp
EMAIL_FROM=vision@<domain>
EMAIL_TO=<finalert address>
# provider-specific keys...

# Brahmastra / models
BRAHMASTRA_MODE=api|cli
MODEL_GENERATE=...
MODEL_CRITIQUE=...
MODEL_VERIFY=...

# Content
RECENCY_HOURS=48
GROUNDING_MIN_PCT=100
DEDUP_SIM_THRESHOLD=0.80

# Images / visuals (§13.6)
IMAGE_ENABLED=true
IMAGE_MODEL=gpt-image-1|imagen-*|gemini-image-*   # confirm exact ID at build time
IMAGE_MAX_PER_WEEK=4
IMAGE_STYLE_GUIDE="minimal, professional, muted palette, no text, no logos"
CARD_BRAND_PALETTE="navy=#0B1F3A;gold=#C9A24B"     # BRAHMASTRA palette

# Author / signature (§15.6, D9)
POST_SIGNATURE_MODE=card_watermark|off|text_footer|both
POST_SIGNATURE_TEXT="— curated via Brahmastra, my multi-AI system"
BRAHMASTRA_LOGO_PATH=/opt/vision/assets/brahmastra_logo.svg
```

## Appendix B — Example approval email (schematic)
```
Subject: VISION daily draft — AI in clinical ops — 6 Jul 2026

[ PROPOSED POST — 1,523 chars ]
<the exact post text>

[ IMAGE — type: informative-card ]
<inline preview of the rendered stat card (branded, watermarked)>
Regenerate · Swap type · Drop image  (via Edit)

[ QUALITY REPORT ]
Grounding: 100% (3/3 claims sourced) · Dedup vs your last 90d: PASS (max sim 0.31)
Tone flags: none · Compliance flags: none · Confidence: 0.86
Image: informative-card, numbers verified vs sources · Author: Vishnu · Signature: card watermark

[ SOURCES ]
1. <title> — <link>
2. <title> — <link>

[ APPROVE & SCHEDULE 09:00 ]   [ POST NOW ]   [ EDIT ]   [ REJECT ]
Run 7f3a… · Links expire 20:00 IST today.
```

## Appendix C — Example generated post (illustrative shape, not real facts)
```
Hook: Most "AI in healthcare" wins aren't models — they're workflows.

Body: <2–4 short paragraphs blending the day's HC + AI signals, grounded in sources,
written as a pragmatic operator, connecting "what's happening" to "how we leverage it">

Takeaway: One concrete action a healthcare leader/builder can take this week.

#HealthTech #AIinHealthcare #DigitalHealth
```

## Appendix D — Glossary
- **Brahmastra:** owner's multi-model system (Claude/Codex/Gemini), repo `finalertserats-prog/God-Mode-Brahmastra`; used here (via the `BrahmastraClient` adapter, §13.0) as generate→critique→verify.
- **Grounding %:** share of factual claims traceable to an ingested source.
- **Member URN:** `urn:li:person:{sub}` — the author identity for personal posts.
- **STAGING mode:** posts then immediately deletes a marked test post to E2E-validate against the live API.

---

*End of BRD v1.0 — awaiting Section 3 sign-off.*

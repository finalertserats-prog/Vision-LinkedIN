# Session: 2026-08-06 — the silent council outage, lane preflight, and the move to one shared Brahmastra

Started from one question: *"I know this is in VPS but not sure why I did not get the email
today."* Ended with a post live on LinkedIn, three classes of health-check dishonesty fixed,
and VISION running off a centralized Brahmastra core.

## What Was Done

### 1. Diagnosed the missing email (the actual outage)

`vision-council.service` crashed at 02:30 UTC and told nobody. Chain of events:

- The Claude CLI's OAuth token expired on the VPS. `claude -p` printed
  `Failed to authenticate. API Error: 401 OAuth access token has expired.` **to stdout**.
- `Voices.ask` (`src/vision/council/voices.py`) gates success on "stdout is non-empty", so
  that sentence travelled upstream **as a model answer**. It became the day's *topic*, then
  failed the composer 3× under the misleading label `parse miss: empty or too-short post body`.
- Everything fail-closed correctly, systemd marked the unit `failed` — and the chain stopped.
  The only symptom was a missing approval email.

**Why the token died:** the deploy had *copied* `.credentials.json` from `/root` to
`/home/vision`. OAuth tokens rotate on refresh, so two homes refreshing independently
invalidate each other — root's read `revoked`, vision's read `expired`.

### 2. Three fixes shipped (VISION `6c33da1`, `2a590bd`)

**A CLI diagnostic is not content** — `detect_cli_error()` in `src/vision/council/voices.py`.
Output that is BOTH short (≤400 chars) AND carries a known diagnostic signature is treated as
a dead lane (fail-soft), not an answer. The length ceiling is load-bearing: a long post
*about* expired tokens passes through untouched. Signatures use the punctuated forms tools
actually emit (`": command not found"`, not the bare English phrase).
Tests: `tests/test_council_voices.py` (12), incident output pinned as a fixture.

**A failed unit reports itself** — `src/vision/ops/job_failure.py` +
`deploy/systemd/vision-job-failed@.service`, wired via `OnFailure=vision-job-failed@%n.service`
into all 8 job units. Mails the failed unit's own log tail through the existing
`vision.ops.alerts` seam. Subject is timestamp-free so the dedup window can suppress a flap.
Tests: `tests/test_job_failure_alert.py` (16).

**Ask before spending** — `src/vision/ops/preflight.py` + `vision-preflight.{service,timer}`.
Runs Brahmastra's mesh health 30 min before the council (07:30 IST vs 08:00) and alerts on any
dead lane; the council re-checks at start and blocks in ~24s when the COMPOSER lane is down
instead of burning 2 minutes to crash. Tests: `tests/test_preflight.py` (27).

### 3. Brahmastra health was lying in BOTH directions (`afbcfe6` in God-Mode-Brahmastra)

`godmode-mesh-health.sh` had all three faults live at once on the VPS:

| Lane | Reported | Actually | Cause |
|---|---|---|---|
| claude | UP | **dead (401)** | `claude_ok=1` hardcoded, `"ok": true` written unconditionally ("we are Claude") |
| gemini | DOWN | fine | probed the sunset `gemini_call.sh`, not `agy_call.sh` |
| codex | DOWN | fine | `codex_call.sh` lacked `--skip-git-repo-check`; VPS copy also predated the 2026-07-07 short-reply gate |

Plus `mesh-status.json` — the file whose stated job is being polled by other tools — was
**invalid JSON whenever a lane failed** (raw multi-line probe output interpolated into JSON
strings). Now goes through `json_escape()`. Claude also now counts toward the unhealthy tally
and can exit 1; it was previously excluded from the arithmetic entirely.

### 4. Migrated VISION onto the shared Brahmastra core

Another session built `/opt/brahmastra` (one install, one auth, `brahmastra` group,
`CLAUDE_CONFIG_DIR` via `/etc/profile.d/brahmastra.sh`). Getting VISION onto it exposed
**three gaps that blocked every app on the box, not just VISION**:

- **The `claude`/`codex`/`gemini` wrappers exec'd themselves forever.** Self-exclusion used
  `"$HOME/.claude/bin"`, but under a shared core the wrapper lives at
  `/opt/brahmastra/.claude/bin` while `$HOME` is `/home/<app>` — a path that doesn't even
  exist. So the wrapper found itself first on PATH and `exec`ed itself. Because it's an
  `exec`, it stayed ONE process with no children and simply **hung**: no output, no error, no
  exit. Fixed by deriving `self_dir` from `$0`.
- **No Codex auth in the shared core at all** — only Claude's had been shared.
- **`.codex` permissions** — `config.toml` at mode `600` made codex die in 37ms with
  `Permission denied` before any network call.

### 5. Ran the full pipeline and posted

council → draft → approval email → owner approval → scheduled → `HTTP 201 Created` → live.
**https://www.linkedin.com/feed/update/urn:li:share:7491225375578996736**

## Key Decisions Made

- **Only a dead COMPOSER lane blocks the council.** Claude writes the post, so without it the
  run has one possible ending. Gemini or Codex down still leaves a genuine deliberation, so it
  proceeds degraded (and today's post shipped exactly that way, 2-of-3).
- **An indeterminate preflight never blocks.** A health check must not become a new way to
  lose the daily post. Verified in practice — when the status path mismatched, the preflight
  correctly said "indeterminate, council will run unguarded" instead of guessing.
- **Preflight is inert outside `live` mode.** It drives three real model CLIs; a dry_run probe
  made the test suite call live models (caught by suite runtime tripling 47s → 132s).
- **The council's blocked exit is non-zero and raises NO alert of its own** — the failed unit
  trips `OnFailure=`, which mails the log tail. The owner may see two messages on a dead-Claude
  morning (07:30 "lane down, act now", 08:00 "council did not run"); that is intentional, they
  are different facts and the second is the consequence the first warned about.
- **Share the CODE, share ONE credential — never copies.** The outage was caused by copies,
  not by sharing. A single shared file cannot rotate itself out from under another.
- **Shared auth store must be a NEUTRAL path, not `/root/.claude`.** The CLIs rewrite the dir
  on token refresh, so every app user needs write access; granting that on `/root` would force
  the app units to drop `User=`/`NoNewPrivileges`/`ProtectSystem` hardening.
- **Rejected Codex's suggestion to gate lane-death on exit codes.** Direct same-day evidence
  that exit codes are unreliable in BOTH directions: `codex_call.sh` exited 0 while printing an
  error, and the wrappers exit non-zero while emitting valid answers.

## What's Pending / Next Steps

- **The shared-core permission fixes are live on the box but in NO repo.** Rebuilding
  `/opt/brahmastra` would lose them. Needs folding into the shared install script:
  - `/opt/brahmastra/.codex` → `chgrp -R brahmastra`, dirs `2770`
  - `/opt/brahmastra/.codex/config.toml` → `660 root:brahmastra` (was `600`; note codex appears
    to rewrite this file and reset the mode — worth a guard)
  - Codex `auth.json` present in the shared core at all
- **`/opt/vision/.env` additions are not in git either** (env files never are):
  `BRAHMASTRA_COUNCIL_DIR=/opt/brahmastra/.claude/council`,
  `BRAHMASTRA_SCRIPTS_DIR=/opt/brahmastra/.claude/scripts`,
  `CLAUDE_CONFIG_DIR=/opt/brahmastra/.claude`, `CODEX_HOME=/opt/brahmastra/.codex`.
  Backup at `/opt/vision/.env.bak-20260807`. Should be documented in `deploy/DEPLOY.md`.
- **`/home/vision/.claude/council/codex_call.sh` was patched IN PLACE** (backups
  `*.bak-20260806`) because the VPS council scripts are an older generation than the repo — the
  repo's `codex_call.sh` needs `lib_prompt.sh`, absent there. Now largely moot since VISION
  points at the shared council, but the stale per-user copy still exists.
- **`deploy.sh` updates itself mid-run**, so a newly added `systemctl enable` line does not
  execute on the deploy that introduces it. Armed `vision-preflight.timer` by hand; it will
  work normally from the next deploy.
- **Residual risk in `detect_cli_error`:** `hashtags.py` legitimately returns short output, so
  the 400-char ceiling does not shield that call site. Signatures are punctuated machine syntax
  so a collision is far-fetched, but not impossible.
- **Root's Claude auth was refreshed today; `vision`'s private one was not** — now irrelevant
  since VISION reads the shared core, but `/home/vision/.claude/.credentials.json` is stale
  and could confuse a future debugger.
- Tomorrow (2026-08-07) is the first unattended run of the full new chain. All three lanes were
  verified green in the **exact systemd unit environment** (claude 5.6s, gemini 14.1s, codex
  6.5s), so it should be a 3-voice post.

## Patterns Learned

- **A health check that is wrong in both directions is worse than none** — it launders
  "I didn't check" into "it's fine". `heartbeat.js` reported claude UP at 0ms latency, 100%
  uptime, 20 checks, while a real call died on a revoked token.
- **"We are X, so X must be healthy" is only true interactively.** Under cron/systemd every
  lane is a shelled-out dependency, including the one you think you are.
- **An `exec` loop presents as a hang, not a crash** — one process, no children, no output, no
  error, no exit. Nothing in the logs. Look for a wrapper resolving its own name on PATH.
- **Test harnesses lie too.** `systemd-run --uid=X --gid=X` does NOT apply supplementary
  groups; a real `User=` unit does. That sent me down a wrong path until I reproduced the unit
  faithfully.
- **Verify the probe by pointing it at something you have independently confirmed is broken.**
  Every fix here was proven against the live 401, not against a mock.
- **Freshness matters for any status file you did not just write.** A leftover
  `mesh-status.json` would have blocked a recovered council (stale "down") or masked a live
  outage (stale "healthy"). Guard on mtime vs. run start.
- **Windows checkout → Linux VPS: always `tr -d '\r'`.** Bit twice this session (`$'\r':
  command not found`), and it is already a documented `.env`/systemd trap.

## Files Changed

**VISION (`Vision-LinkedIN`, commits `6c33da1`, `203b5e7`, `2a590bd`)**
- `src/vision/council/voices.py` — `detect_cli_error()` + dead-lane guard in `ask()`
- `src/vision/ops/job_failure.py` — NEW, `vision-alert-failure` OnFailure handler
- `src/vision/ops/preflight.py` — NEW, lane preflight + `vision-preflight`
- `src/vision/cli/council.py` — preflight gate in `main()`
- `src/vision/config.py` — `brahmastra_scripts_dir`
- `pyproject.toml` — `vision-alert-failure`, `vision-preflight` console scripts
- `deploy/systemd/vision-job-failed@.service`, `vision-preflight.{service,timer}` — NEW
- `deploy/systemd/vision-{council,daily,expire,publisher,token,retention,web}.service` — `OnFailure=`
- `deploy/deploy.sh` — arm `vision-preflight.timer`
- `tests/test_council_voices.py`, `test_job_failure_alert.py`, `test_preflight.py` — NEW
- `tests/test_deploy_smoke.py` — OnFailure + preflight-ordering guards
- `notebook/.meta/team-context.md` — shared-Brahmastra findings for the other session

**God-Mode-Brahmastra (commits `afbcfe6`, `f95529e`, + wrapper fixes)**
- `global/.claude/scripts/godmode-mesh-health.sh` — real claude probe, agy lane, `json_escape()`, exit codes
- `global/.claude/council/codex_call.sh` — `--skip-git-repo-check`
- `global/.claude/bin/{claude,codex,gemini}` — self-exclusion by `$0`, not `$HOME`

**Final state:** 694 tests pass, ruff clean, three Codex review rounds (caught a real
arbitrary-file-read in the failure handler and a stale-status hole in the preflight — both
fixed and regression-tested).

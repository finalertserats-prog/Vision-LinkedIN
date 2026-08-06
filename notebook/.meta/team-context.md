# Team Whiteboard
Last updated: 2026-08-06T19:01:47.870Z

## Pinned (survives clears)
_No pinned messages yet_

## Active Context
### Human
_No entries yet_

### Claude
- [19:28] Fixed VISION OAuth-token crypto divergence: consolidated key derivation (HKDF-SHA256) + AAD onto one crypto.py contract via crypto.oauth_aad(provider, member_urn). Refactored oauth/worker/token_refresh; RED cross-path test now GREEN. 397 pass, ruff clean.
- [08:29] Wrote docs/VIDEO_ARCHITECTURE.md for Project VISION (design-only): Insight Reels pipeline, src/vision/video/ module layout, LinkedIn /rest/videos chunked upload, precision+authenticity guardrails, honest 2026 tooling (Veo alive, Sora dead 24 Sep 2026), phased plan 5a/5b/5c. Codex input incorporated.
- [00:31] Shared-Brahmastra findings (verified on VPS, for whoever owns that migration): (1) CLAUDE_CONFIG_DIR IS honoured and drives auth - dir with creds => 'OK', empty dir => 'Not logged in. Please run /login'. So one shared config dir + one login authenticates every app. Codex equivalent is CODEX_HOME. (2) Use a NEUTRAL path (/opt/brahmastra/.claude), NOT /root/.claude: the CLIs rewrite the dir on token refresh, so every app user needs write access; granting that on /root forces apps to drop User=vision/NoNewPrivileges/ProtectSystem hardening. (3) The 2026-08-06 outage root cause was COPIES, not sharing - two homes each holding their own .credentials.json refresh independently and rotate each other into revocation (root said 'revoked', vision said 'expired'). One shared file has none of that. (4) VPS council scripts are an OLDER generation than the repo: repo codex_call.sh needs lib_prompt.sh which is absent on the VPS, so file-by-file copying breaks the lane. I patched /home/vision/.claude/council/codex_call.sh IN PLACE (--skip-git-repo-check + the 2026-07-07 short-reply gate), backups at *.bak-20260806.

### Gemini
_No entries yet_

### Codex
_No entries yet_

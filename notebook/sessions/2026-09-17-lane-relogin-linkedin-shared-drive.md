# Session: 2026-09-17 - 13-day council outage, re-logins, LinkedIn reminder, shared Drive backups

## What Was Done
- **Diagnosed the `vision-council.service` failure alert.** Preflight had blocked every run from 2026-09-05 to 09-17:
  - Claude's OAuth token expired 09-04 10:00 UTC, and the refresh failed even though a refresh token was stored.
  - Codex's login had expired too.
  - Codex was ALSO locked out because `/opt/brahmastra/.codex/config.toml` was `root:600` again (ACL mask `---`).
  - Gemini (agy) was healthy the whole time.
- **Re-logged Claude and Codex on the VPS.** Each ran as `vision`, with CLAUDE_CONFIG_DIR / CODEX_HOME set, from a tmux session driven over ssh:
  - Codex: `codex login --device-auth`
  - Claude: interactive `claude`, then `/login`
- **Ran the council by hand.** All 3 voices took part and the approval email was sent.
- **Codex config permission fix (`6ce81d1`, later generalised in `3cf44e2`).** A systemd path unit re-applies `chmod 660` whenever the file is rewritten:
  - Hardened with `find -P -type f` and a `ProtectSystem=strict` sandbox limited to the watched directories.
  - Tested against an atomic-rename rewrite, a re-arm (second rewrite), and a chmod attempt outside the sandbox (`/etc/hostname`), which was blocked.
- **Found the LinkedIn problem.** The owner approved the draft, but LinkedIn returned 401 with "no refresh token stored".
  - Re-authorised with `scripts/authorize_linkedin.py` on the VPS; the consent redirect was read from the owner's Chrome tab.
  - The post was published: `urn:li:share:7506353189215203329`.
- **LinkedIn expiry reminder (`ae2b314`):**
  - `vision-token.timer` was never enabled; `deploy.sh` now enables it on every deploy.
  - The re-auth alert now states the exact access-token expiry time (TDD: `tests/test_token_refresh.py::test_reauth_reminder_states_when_the_access_token_expires`).
  - The docstring in `scripts/authorize_linkedin.py` was corrected: the token lasts ~60 days, with no refresh token.
- **Claude one-year token.** `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) is in `/opt/vision/.env` and expires ~2027-09-17. It was verified alone, with an empty CLAUDE_CONFIG_DIR.
- **Google Drive backups, activated and shared across the box (`3cf44e2`):**
  - rclone v1.75.1 installed from the official .deb, checksum verified.
  - Google Cloud project `brahmastra-drive`:
    - Drive API enabled.
    - External consent screen **In production**.
    - Desktop client `brahmastra-rclone-vps`.
    - Scope `drive.file`.
  - Homepage and privacy policy on GitHub Pages: https://finalertserats-prog.github.io/brahmastra-drive/ (repo `finalertserats-prog/brahmastra-drive`).
  - The shared remote `gdrive` lives in `/opt/brahmastra/.config/rclone/rclone.conf` (directory `root:brahmastra 2770`).
  - `RCLONE_CONFIG` is set in `/etc/profile.d/brahmastra.sh` and in `/opt/vision/.env`, along with `RCLONE_REMOTE=gdrive`.
  - The watcher was renamed `brahmastra-shared-perms` and now guards both the codex config and the rclone config.
  - Verified with an upload plus `check --one-way` as `vision` under systemd; the token refresh rewrote the file and the watcher restored `660`.
- **All three copies match.** Local, GitHub and the VPS are on `3cf44e2`, with no unit drift and no failed units. `deploy/DEPLOY.md` §5 documents how to rebuild the Drive setup.

- **End-of-day verification (after the first session-end):**
  - **Dry run:** a council run with `VISION_ENV=dry_run`, launched via systemd-run with a wrapper script that exports it, and an explicit `--topic` so no queue item was consumed.
    - All 3 voices took part, a diagram image was attached, and the approval email was suppressed.
    - `vision-publisher` and `vision-token` were also dry-run: 0 drafts due, and the LinkedIn token reported healthy.
  - **Dry-run draft `ac560bbb` rejected.** It was moved `pending_approval -> rejected` through `vision.approval.state_machine.transition` (actor owner, audited), not a row delete. It was the only pending draft.
  - **Final live test:**
    - `systemctl start vision-council.service` took the topic from the queue (private equity and nursing homes), attached an art image, and sent the approval email.
    - The owner approved from the email.
    - The publisher posted at 15:15:32 UTC: `urn:li:share:7506370262838431744`, followed by the "post is live" email.
    - The whole daily loop is proven end to end, and the schedule takes over from 2026-09-18.

## Key Decisions Made
- **Reject the dry-run draft; don't delete the row.** REJECTED is terminal, so the draft can never be approved or published, and the audit trail and foreign keys stay intact.
- **Watch the rewrite (path unit), not a schedule or `ExecStartPre`.** The one-off chmod held five weeks, then silently regressed. The watcher also covers every app on the box, not just VISION's runs.
- **Harden the root chmod.** The directories are group-writable, so a symlink swap could have made root chmod `/etc/shadow`. Guarded by `find -P` plus a sandbox (a Codex review finding).
- **Enable `vision-token` rather than building a new reminder.** The job already emails a re-auth alert in the final 7 days when no refresh token exists. It was simply never armed.
- **Use a one-year Claude token instead of the shared OAuth file.** The shared file's refresh failed silently on 09-04. The env token takes priority over the file.
- **Use the owner's own Google OAuth client.** rclone's shared client is "being retired during 2026".
  - `drive.file` scope for least privilege.
  - Production publishing, because Testing expires tokens every 7 days.
  - GitHub Pages for the required privacy page (the owner's choice, over adding a page to finalert.tech's production nginx).
- **Keep the owner's email off the public privacy page.** Google's consent screen already shows the support address.

## What's Pending / Next Steps
- **First real Drive backup:** Saturday 22:00 UTC (`vision-retention`). Check `logs/vision-retention.log` for `backed_up: true`.
- **LinkedIn token:** expires ~mid-Nov 2026. Daily reminder emails start 7 days before. Re-auth with `authorize_linkedin.py` on the VPS; see memory `linkedin-token-expiry`.
- **Revoke the old "rclone" (shared client) grant** at myaccount.google.com/permissions. It's the owner's action.
- **Optionally revoke the corrupted first Claude setup-token** (the older of the two issued today) in claude.ai settings.
- **Other VPS apps (e.g. Finalert, running as root via cron)** need `RCLONE_CONFIG` exported in their own environment to use `gdrive`.
- **Codex warns about malformed agent files** in `/opt/brahmastra/.codex/agents/.broken-2026-05-28/`. They're harmless and could be moved aside.
- **Unexplained:** why Claude's shared-file refresh failed on 09-04, and why 12 days of alert emails went unactioned.

## Council Sessions (if any)
- None. Codex review passes were run on each change:
  - It raised the symlink-hardening finding, which was applied.
  - Its remaining findings were assessed and rejected with reasons: the deploy fail-closed behaviour is deliberate, and the unescaped parens in `ExecStart` were verified to work on the box.

- **The dry run's diagram used up the diagram cooldown** ("0 of 4 posts"), so the next 4 real posts will use art. That's harmless and fits the art-first rule, but a dry run should ideally not touch the visual-variety state.
- **First scheduled run:** 2026-09-18, preflight at 02:00 UTC and council at 02:30 UTC. Confirm the approval email arrives.

## Patterns Learned
- **A council dry run still writes real state.** It stores a `pending_approval` draft (visible in the web approval UI) and advances the diagram cooldown. Afterwards, reject the draft through the state machine.
- **`deploy.sh` runs the OLD copy of itself** on the pass that pulls a new version. `git pull` first, or run it twice.
- **Extracting a token from tmux:** never `tr -d "\n"` before grepping, because the next line gets glued on (130 chars instead of 108, giving 401 invalid).
  - Grep per line on `capture-pane -J`.
  - Write straight to a root-only file, and test before installing.
- **Testing env inside systemd:**
  - `systemd-run` expands `$VAR` / `${...}` in argv, so use a script file.
  - EnvironmentFile overrides `-E`.
  - `bash -l` re-sources `/etc/profile.d` and silently resets CLAUDE_CONFIG_DIR.
- **An env token takes priority over the credentials file.** A BAD `CLAUDE_CODE_OAUTH_TOKEN` breaks the lane even when the file is fine.
- **rclone and codex both save their config as 0600** (write, then rename). A shared credential file needs a watcher, not a one-off chmod.
- **TaskStop on a background `ssh -L`** kills the bash wrapper but can leave `ssh.exe` holding the port; it keeps forwarding.
- **The Chrome extension may refuse `127.0.0.1` navigation in some tabs.** Retry in another tab of the group.
- **Google Auth Platform now requires homepage and privacy-policy URLs** (plus an authorized domain) before "Publish app" is enabled.
- **The secret-redactor hook false-positives** on log lines mentioning "token" and on test placeholders.

## Files Changed
- `deploy/systemd/brahmastra-shared-perms.path`: new; replaces `brahmastra-codex-perms.path`.
- `deploy/systemd/brahmastra-shared-perms.service`: new; replaces `brahmastra-codex-perms.service`.
- `deploy/deploy.sh`:
  - installs `*.path` units
  - enables `vision-token.timer` and `brahmastra-shared-perms.path`
- `deploy/DEPLOY.md`: §5 rewritten for the shared Drive remote.
- `src/vision/publish/token_refresh.py`: the re-auth reason includes the access-token expiry.
- `tests/test_token_refresh.py`: new reminder test (695 tests pass).
- `scripts/authorize_linkedin.py`: docstring (60-day token, VPS usage).
- `.gitignore`: `client_secret*.json`.
- **VPS only, not in git:**
  - `/opt/vision/.env`: RCLONE_REMOTE, RCLONE_CONFIG, CLAUDE_CODE_OAUTH_TOKEN. Backups: `.env.bak-20260917`, `.env.bak-20260917b`.
  - `/etc/profile.d/brahmastra.sh`: RCLONE_CONFIG.
  - `/opt/brahmastra/.config/rclone/`: new directory and config.
  - `/root/.brahmastra-secrets/`: OAuth client JSON and `rc-create.sh`.
  - `/root/unit-backup-20260917/`: old units, the profile backup, and the old shared-client rclone.conf.
  - `/root/codex-config.toml.bak-20260917`.
- **External:**
  - GitHub repo `finalertserats-prog/brahmastra-drive` (Pages).
  - Google Cloud project `brahmastra-drive`.
- **Commits:** `6ce81d1`, `ae2b314`, `3cf44e2`.

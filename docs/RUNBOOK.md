# VISION — Operational Runbook

Documented recovery procedures for the on-call operator (BRD §17). Every
procedure is **fail-closed**: when in doubt, stop — a missed post is always
cheaper than a wrong or duplicated one.

> **Conventions used below**
> - `$VISION` = deploy root on the VPS (e.g. `/root/Projects/Vision-LinkedIN`).
> - `$PY` = the venv Python: `$VISION/.venv/bin/python`.
> - All jobs read config from `$VISION/.env` (see Appendix A of the BRD). Never
>   paste secrets into a shell, a ticket, or a chat — reference the env var name.
> - Console scripts (from `pyproject.toml`): `vision-daily`, `vision-publisher`,
>   `vision-token`, `vision-expire`; the always-on web service is
>   `vision.approval.web:create_app` served by uvicorn behind the TLS proxy.
> - The draft state machine (BRD §10.4):
>   `new → drafted → pending_approval → approved → queued → published`, with
>   `rejected` / `expired` / `dead_letter` as terminal side-states.

---

## 0. Before you touch anything

1. Confirm the symptom from an alert email or `/healthz` — do not act on a hunch.
   ```bash
   curl -fsS https://<vps-domain>/healthz | jq .
   ```
   A healthy body reports `status: ok`, `db: ok`, `token_secret: configured`.
   `503` means the DB is unreachable (readiness fails closed) — see §6 Restore.
2. Check recent structured logs for the correlated `run_id`:
   ```bash
   journalctl -u vision-web -u vision-publisher -u vision-daily --since "1 hour ago" --no-pager
   ```
3. Take a backup **before** any state-changing recovery (see §6).

---

## 1. Re-authorise LinkedIn (401 → re-auth alert)

**When:** you receive a *"VISION — LinkedIn re-authorisation required"* alert, or
`vision-token` logs `re-auth needed`, or a publish reverts a draft to `scheduled`
with no `post_urn`. The refresh token has expired/been revoked, so the publisher
cannot mint a new access token on its own.

**Invariant preserved by the system:** the approved draft is **kept intact** and
re-publishes automatically once access is restored — you do **not** need to
re-approve or re-generate anything.

**Steps**

1. Verify it is genuinely an auth problem (a 401, not a 403 scope/role issue —
   a 403 is a config fix, not a re-auth):
   ```bash
   journalctl -u vision-publisher --since "2 hours ago" --no-pager | grep -i reauth
   ```
2. Start the one-time authorize flow. The redirect URI must exactly match the
   LinkedIn app config: `https://<vps-domain>/oauth/linkedin/callback` (BRD §15.1).
   The OAuth glue lives in `vision.publish.oauth` (`start_authorize` builds the
   URL with a CSRF `state`; `handle_callback` verifies `state`, exchanges the
   code, and stores the **encrypted** access + refresh tokens + `member_urn`).
   ```bash
   # Generates the login URL (owner opens it in a browser and signs in).
   $PY -c "import secrets; from vision.publish.oauth import start_authorize; \
           print(start_authorize(secrets.token_urlsafe(16)))"
   ```
3. The owner logs in and approves the `w_member_social openid profile email`
   scopes (least privilege — request nothing more). LinkedIn redirects to the
   callback, which persists the new encrypted tokens to `oauth_tokens`.
4. Confirm the token row updated and access no longer near expiry:
   ```bash
   $PY -m vision.cli.token   # vision-token: refreshes if in-window, else reports OK
   ```
5. Let the next `vision-publisher` poll (≤5 min) re-drive the preserved draft, or
   trigger it once manually:
   ```bash
   systemctl start vision-publisher   # oneshot poll
   ```
6. Confirm the confirmation email arrives and the draft reached `published`.

**Never** delete or re-create the approved draft to "force" a re-post — that is
how double posts happen. The idempotency guard (below) is what protects you.

---

## 2. Backfill a missed day

**When:** the `06:30` daily run did not produce a draft (VPS was down, feeds all
failed, synthesis outage) and no approval email arrived.

**Steps**

1. Check whether a `runs` row exists for today and its status:
   ```bash
   $PY -c "from vision.db.session import get_session; from vision.db.models import Run; \
           from sqlalchemy import select; \
           s=next(get_session().__enter__() for _ in [0]); \
           print([(str(r.id), r.status, r.created_at) for r in s.execute(select(Run).order_by(Run.created_at.desc()).limit(3)).scalars()])"
   ```
   - `status = partial` → synthesis failed loudly (a fault was injected/hit). Fix
     the upstream cause (see §1/§4) then re-run.
   - no row → the job never started (check cron/systemd timer + `journalctl`).
2. Re-run the daily pipeline manually. It is safe to re-run: a fresh run produces
   a **new** draft; it does not touch already-published drafts.
   ```bash
   $PY -m vision.cli.daily     # vision-daily: ingest → curate → synthesise → email
   ```
3. If you must backfill **yesterday** specifically (rare), run the daily job — the
   ingest recency window (`RECENCY_HOURS`, default 48h) still surfaces recent
   signals. Do **not** hand-edit historical `runs`/`drafts` rows.
4. Watch the approval email land, then proceed through the normal approve flow.

**Guardrail:** never run two `vision-daily` processes concurrently — the design
assumes one daily run. If a manual run overlaps the cron, cancel one.

---

## 3. Replay a failed publish (dead_letter)

**When:** a draft is in `dead_letter` (retries exhausted on 429, or a terminal
failure) and you have fixed the root cause (rate limit passed, config corrected).

**Understand first:** `dead_letter` is terminal *by design* so the poller never
re-attempts it automatically. A 429-driven dead_letter is **safe to replay**
because a 429 proves LinkedIn never created a post. A dead_letter from an *unknown
outcome* is **not** replayable until you reconcile (see the caution).

**Steps**

1. Identify the dead-lettered drafts:
   ```bash
   $PY -c "from vision.db.session import get_session; from vision.db.models import Draft; \
           from sqlalchemy import select; \
           s=next(get_session().__enter__() for _ in [0]); \
           print([(str(d.id), d.state, d.post_urn) for d in s.execute(select(Draft).where(Draft.state=='dead_letter')).scalars()])"
   ```
2. **Reconcile before replay (mandatory):** confirm no post already exists for the
   draft text on the owner's profile. The publisher's own reconcile seam wraps
   `LinkedInClient.find_existing_post` — if a post exists, **adopt** it instead of
   re-posting (set `post_urn` from the live post; do not create a new one).
3. Only if reconciliation confirms *no* post exists, replay it. `dead_letter` is
   terminal in the state machine (`ALLOWED_TRANSITIONS[DEAD_LETTER]` is empty) — by
   policy the poller must never resurrect it on its own. The clean, audit-safe
   replay is therefore **re-approval of a freshly generated draft**, not hand-
   mutating a terminal row:
   - `reject` the dead-lettered draft (records the decision in `audit_log`),
   - run `vision-daily` to generate a new draft for the same signals,
   - approve the new one through the normal email flow.

   This cannot double-post (the old draft is terminal; the new one has its own
   idempotency key) and leaves a clean trail. Only a break-glass operator, after
   documented reconciliation, should ever force a terminal row back to `queued`.
4. If you did re-queue, trigger one poll and confirm publication:
   ```bash
   systemctl start vision-publisher
   ```

> **Caution — never re-post on ambiguity.** If reconciliation *fails* (LinkedIn
> lookup errors), stop. Leave the draft as-is and escalate. The publisher already
> fails closed on this; your manual actions must too.

---

## 4. Disable a bad feed

**When:** a `sources.last_ok_at` staleness alert fires, or a feed emits garbage /
starts 403ing / floods low-quality items. One dead feed never aborts a run
(ingest isolates per-source), but a misbehaving one should be quiesced.

**Steps**

1. Identify the offending source and its health:
   ```bash
   $PY -c "from vision.db.session import get_session; from vision.db.models import Source; \
           from sqlalchemy import select; \
           s=next(get_session().__enter__() for _ in [0]); \
           print([(x.name, x.enabled, x.last_ok_at) for x in s.execute(select(Source)).scalars()])"
   ```
2. Disable it (config over code — a data flag, not a code change):
   ```bash
   $PY -c "from vision.db.session import get_session; from vision.db.models import Source; \
           from sqlalchemy import select; \
           s=next(get_session().__enter__() for _ in [0]); \
           src=s.execute(select(Source).where(Source.name=='<SOURCE_NAME>')).scalar_one(); \
           src.enabled=False; s.commit(); print('disabled', src.name)"
   ```
   Only `enabled=True` sources are fetched (`get_enabled_sources`), so the next
   daily run skips it cleanly. No restart required.
3. Re-enable later by setting `enabled=True`. To add or swap a feed, edit
   `prep/sources_seed.yaml` and re-run the seed upsert — never hard-code feeds.

---

## 5. Rotate secrets

**When:** on a schedule, on suspected compromise, or when a team member with
access leaves. Covers `SECRET_HMAC_KEY` (approval-link signing), `TOKEN_ENC_KEY`
(OAuth token envelope), `EMAIL_API_KEY`, and the LinkedIn client secret.

**Order matters — rotate fail-closed, one secret at a time.**

1. **HMAC key (`SECRET_HMAC_KEY`)** — signs approval links. Rotating it invalidates
   every outstanding approval link (they fail verification = fail closed, which is
   correct). Steps: set the new value in `.env`, restart `vision-web`, then let the
   next `vision-daily` mint fresh links. Old links now return the generic error
   page — expected.
2. **Token encryption key (`TOKEN_ENC_KEY`)** — wraps OAuth tokens at rest. The
   envelope is versioned, so support re-encryption rather than a hard swap:
   - Provision the new key alongside the old (KMS/secret manager, **never** beside
     the ciphertext in the DB).
   - Re-encrypt existing `oauth_tokens` rows under the new key, or simply
     **re-authorise LinkedIn** (§1) which writes fresh ciphertext under the new key.
   - Restart `vision-publisher` / `vision-token` so they load the new key.
3. **Email API key (`EMAIL_API_KEY`)** — set new value, restart the web +
   publisher (they lazily build the sender), send a test alert to confirm delivery.
4. **LinkedIn client secret** — rotate in the LinkedIn developer app, update
   `LI_CLIENT_SECRET`, then re-authorise (§1).

**After any rotation:** confirm `/healthz` shows `token_secret: configured` (never
the insecure dev default), send a test approval email end-to-end, and verify no
secret leaked into logs (`journalctl ... | grep -Ei 'bearer|token=|secret'` should
return nothing — logs are redacted by design).

---

## 6. Restore from backup

**When:** DB corruption, accidental data loss, or migrating the VPS. Nightly
`pg_dump` of the `vision` schema runs at `02:00` and retains 14 days (adapted from
finalert's `backup.py` pattern: dump + timestamped file + retention prune).

**Steps**

1. **Stop writers first** so nothing races the restore (fail closed):
   ```bash
   systemctl stop vision-web vision-publisher vision-token
   ```
2. List available backups (newest first) and pick the target:
   ```bash
   ls -1t $VISION/backups/vision_*.sql.gz | head
   ```
3. Restore the chosen dump into a **fresh** target (never overwrite the live DB
   blind — restore to a scratch schema/db, verify, then cut over):
   ```bash
   gunzip -c $VISION/backups/vision_<STAMP>.sql.gz | psql "$DATABASE_URL"
   ```
4. Run migrations to bring the restored schema to head (idempotent):
   ```bash
   $VISION/.venv/bin/alembic -c $VISION/alembic.ini upgrade head
   ```
5. Sanity-check row counts and the most recent `runs`/`drafts`/`oauth_tokens`,
   then restart writers:
   ```bash
   systemctl start vision-web vision-publisher vision-token
   curl -fsS https://<vps-domain>/healthz | jq .
   ```
6. **Test restores routinely** (BRD §17: "test restore") — a backup you have never
   restored is a hope, not a backup. Restore into a scratch DB monthly.

> **Publish safety after restore:** if the restore rewinds state, a draft that was
> `published` upstream but restored as `queued`/`approved` must NOT be blind
> re-published. The publisher reconciles (`find_existing_post`) before any create
> and adopts an existing post — but verify the owner's recent LinkedIn posts
> manually after any restore that crosses a publish boundary.

---

## Appendix — quick command reference

| Task | Command |
|---|---|
| Health | `curl -fsS https://<vps-domain>/healthz \| jq .` |
| Run daily pipeline now | `$PY -m vision.cli.daily` |
| Poll + publish approved/due | `systemctl start vision-publisher` |
| Refresh/inspect tokens | `$PY -m vision.cli.token` |
| Expire un-actioned drafts | `$PY -m vision.cli.expire` |
| Tail all service logs | `journalctl -u vision-* --since "1 hour ago" --no-pager` |
| Backup now | `$PY $VISION/scripts/backup.py` (pg_dump + retain 14d) |

All state changes are recorded append-only in `audit_log` (actor + action +
timestamp + nonce hash — never a raw token). When escalating, attach the relevant
`run_id` / `draft_id` and the audit rows, not credentials.

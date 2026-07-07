# Spikes

De-risking scripts run in **Phase 0** to prove the riskiest externals work
before building the real pipeline (BRD §20, Phase 0). These are throwaway,
runnable probes — not production code — kept in the repo so the setup is
reproducible.

The following spikes are added in later Phase 0 steps (by dedicated agents):

| Spike | Purpose (BRD ref) |
|---|---|
| `spike_brahmastra.py` | Confirm the `BrahmastraClient` adapter returns schema-valid JSON for generate/critique/verify via the local council CLI scripts, and that per-pass model routing works where supported (§13.0, §13.1). |
| `spike_linkedin.py` | One-time OAuth, then publish a single "hello world" test post via `/rest/posts` and delete it, proving the `w_member_social` path end-to-end (§6, §15). |
| `spike_email.py` | Send a test approval email carrying a working signed, single-use link that flips a DB flag when clicked (§14.2). |

## Running

Spikes are executed directly, e.g. `python spikes/spike_linkedin.py`, with a
populated `.env` (copy from `.env.example`). They read configuration through
`vision.config.get_settings()` so they honour the same env as the app.

> Note: spikes may touch live external systems (LinkedIn, email provider). Run
> them in `staging` mode where possible; the LinkedIn spike posts **then
> immediately deletes** a clearly-marked test post (LinkedIn has no draft state).

### `spike_linkedin.py` — required config

`spike_linkedin.py` needs **real** LinkedIn developer-app credentials in `.env`
before it can run (it drives the live OAuth + Posts API):

- `LI_CLIENT_ID` and `LI_CLIENT_SECRET` — from your LinkedIn app (never commit
  these; `.env` is git-ignored).
- `LI_REDIRECT_URI` — must match an *Authorized redirect URL* on the app exactly.
- `VISION_ENV=staging` — the spike **refuses to run** unless it is in staging, so
  the "hello world" test post is published and then deleted immediately.

The one-time LinkedIn app setup (products/scopes/redirect URL, BRD §15.1) is
documented at the top of `spike_linkedin.py`. The script never logs or CLI-args
the access token, refresh token, or the authorization code — only non-secret
status (member URN, created post URN).

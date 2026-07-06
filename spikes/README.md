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

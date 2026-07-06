# VISION — Daily AI-Assisted LinkedIn Insight Engine

A self-hosted pipeline that ingests fresh **Life-Sciences / Healthcare** and
**AI/technology** signals daily, synthesises a thought-leadership post using the
Brahmastra multi-AI ensemble (**generate → critique → verify**), emails it to the
owner for a proof-read + one-click approval, and — **only on approval** —
publishes it to the owner's personal LinkedIn profile via the official LinkedIn
API.

> Human-in-the-loop by design: nothing is ever posted without the owner reading
> it and clicking **Approve**. See the [BRD](./VISION_BRD_v1.md) for the full
> specification.

## Highlights

- **Fully autonomous daily path** with exactly one intentional human gate (the
  Approve click) — BRD §1.1.
- **Accuracy & precision enforced mechanically**: every factual/numeric claim
  must trace to an ingested source (grounding gate + multi-model verify pass).
- **Precision-first visuals**: numbers/text rendered deterministically (cards/
  charts); diffusion models only for text-free concept illustrations.
- **Self-hosted, no third party** holds the content or LinkedIn tokens.
- **DB-agnostic** data layer: SQLite in dev, PostgreSQL in prod (SQLAlchemy 2.0).
- **CLI-mode Brahmastra**: synthesis routes through the local council scripts
  (`gemini_call.sh`, `codex_call.sh`, …) — no API keys.

## Architecture (at a glance)

```
RSS/APIs → [1] Ingest → [2] Curate → [3] Synthesise (generate→critique→verify)
        → [4] Quality gates → draft → [5] Email → owner Approve
        → [6] Approval service → [7] Publish worker → LinkedIn /rest/posts
        → [8] Confirm + [9] Ops/observability
```

Process model (BRD §10.2):

| Process | Trigger | Role |
|---|---|---|
| `vision-web` | always-on (FastAPI) | `/approve` `/reject` `/edit` `/healthz` |
| `vision-daily` | cron ~06:30 IST | ingest → curate → synthesise → quality → email |
| `vision-publisher` | poller | publish `approved && due` drafts |
| `vision-token` | cron daily | refresh LinkedIn tokens, alert on re-auth |

## Project layout

```
src/vision/
  config.py          # typed settings (BRD Appendix A) — config over code
  logging_setup.py   # JSON logs + run-id correlation + secret redaction
  db/                # SQLAlchemy 2.0 models, session, Alembic migrations
  brahmastra/        # BrahmastraClient adapter over the council CLI (§13.0)
  ingest/ curate/ synthesise/ visuals/   # daily pipeline stages
  mailer/ approval/ publish/             # human-in-the-loop + publishing
  ops/ cli/          # observability + console entry points
tests/               # pytest suite (in-memory SQLite fixtures)
spikes/              # Phase-0 de-risking probes
```

## Quickstart

```bash
# 1. Create a virtual environment and install (editable) with dev extras.
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

# 2. Configure. Copy the template and fill in real values.
cp .env.example .env                 # Windows: copy .env.example .env
#    - Generate TOKEN_ENC_KEY:
#        python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
#    - Set a long random SECRET_HMAC_KEY.

# 3. Create the dev database schema (SQLite by default).
python -c "from vision.db.session import create_all; create_all()"

# 4. Run the tests.
pytest

# 5. Try a console entry point (scaffold stub for now).
vision-daily
```

## Configuration

All configuration is environment-driven (BRD §22, *config over code*). Every
variable is documented in [`.env.example`](./.env.example) and typed on the
`Settings` class in `src/vision/config.py`. Notable defaults are **dev-safe**:
`VISION_ENV=dry_run` (no email/no post), `DATABASE_URL=sqlite:///vision.db`,
`BRAHMASTRA_MODE=cli`.

## Database & migrations

The data layer is DB-agnostic via portable SQLAlchemy types (Uuid, JSON,
JSON-encoded arrays, `DateTime(timezone=True)`, `LargeBinary`). For dev, use
`create_all()`. For prod schema changes, use Alembic:

```bash
alembic revision --autogenerate -m "initial schema"
alembic upgrade head
```

The Alembic environment reads `DATABASE_URL` from settings at runtime
(`alembic.ini` ships without a URL, so no secret is committed).

Semantic-dedup embeddings are stored as a JSON `list[float]` for portability,
with a Python cosine-similarity fallback on SQLite and a documented **pgvector**
path for PostgreSQL in production (see `db/base.py`).

## Status

**Phase 0 scaffold.** Foundation only: config, logging, data layer, tests, and
package skeleton. The Brahmastra client, LinkedIn client, token module, and
pipeline stages are implemented in subsequent phases (BRD §20).

## License

MIT © 2026 Vishnu Dattu Kurnuthala — see [LICENSE](./LICENSE).

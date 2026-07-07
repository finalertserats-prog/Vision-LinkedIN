# VISION — VPS Deployment Runbook

Operational guide for deploying Project VISION to the owner's VPS (Hostinger KVM
/ HP Victus, Ubuntu). VISION is a single-operator system; this runbook assumes
`vishnu` is the sole operator and root/sudo is available.

There are **two supported deploy shapes** — pick one, do not mix:

1. **systemd + host venv** (recommended for a single VPS) — units in
   `deploy/systemd/`, scheduled by systemd timers.
2. **Docker Compose** — `docker-compose.yml`, scheduled by host cron/systemd
   running `docker compose run --rm <job>`.

The daily loop needs nothing beyond the owner's **Approve** click. Everything
below is one-time setup plus rare upkeep (a ~yearly LinkedIn re-auth).

---

## 0. Architecture recap (what runs where)

| Component | Role | Cadence | Surface |
|---|---|---|---|
| `vision-web` | FastAPI approval service | always-on | **only** external surface (behind TLS proxy) |
| `vision-daily` | ingest → synthesise → email draft | 06:30 IST | none |
| `vision-publisher` | publish approved & due drafts | every 5 min | outbound to LinkedIn |
| `vision-token` | proactive OAuth refresh | 02:00 IST | outbound to LinkedIn |
| `vision-expire` | expire un-actioned drafts (fail-closed) | 20:00 IST | none |
| `vision-canary` | probe `/healthz`, alert on failure | every 60 s | loopback only |
| `scripts/backup.py` | nightly `pg_dump` of `vision` schema, retain 14 | 02:00 IST | none |

Security posture (see `prep/security_threatmodel.md`): the approval endpoints are
the **only** thing exposed. `vision-web` binds to `127.0.0.1:8000`; the existing
reverse proxy terminates TLS and is the sole path from the internet. Everything
runs as an unprivileged `vision` user; secrets are injected at runtime, never
baked into images or unit files.

---

## 1. One-time host setup (systemd path)

```bash
# 1. Create the unprivileged service account and layout.
sudo useradd --system --create-home --home-dir /opt/vision --shell /usr/sbin/nologin vision
sudo mkdir -p /opt/vision/logs /opt/vision/backups
sudo chown -R vision:vision /opt/vision

# 2. Clone the repo into the working dir and build a venv.
sudo -u vision git clone https://github.com/finalertserats-prog/Vision-LinkedIN.git /opt/vision/repo
sudo -u vision python3.11 -m venv /opt/vision/venv
sudo -u vision /opt/vision/venv/bin/pip install --upgrade pip
sudo -u vision /opt/vision/venv/bin/pip install -e /opt/vision/repo

# NOTE: the systemd units use WorkingDirectory=/opt/vision and
# ExecStart=/opt/vision/venv/bin/...  Symlink or clone so the code lives where
# the units expect (adjust units if you prefer /opt/vision/repo as WorkingDirectory).
```

### Postgres (`vision` schema)

Reuse an existing Postgres instance or run the pgvector container. Create the DB
and the least-privilege role, then run migrations:

```bash
sudo -u postgres createdb vision
sudo -u postgres psql -c "CREATE ROLE vision LOGIN PASSWORD '<injected-secret>';"
sudo -u postgres psql -d vision -c "CREATE SCHEMA IF NOT EXISTS vision AUTHORIZATION vision;"
sudo -u postgres psql -d vision -c "CREATE EXTENSION IF NOT EXISTS vector;"

# Apply Alembic migrations (from the repo, with DATABASE_URL exported).
sudo -u vision DATABASE_URL="postgresql+psycopg://vision:<secret>@localhost:5432/vision" \
  /opt/vision/venv/bin/alembic -c /opt/vision/repo/alembic.ini upgrade head
```

---

## 2. Secrets injection (never in git, never in images)

Create `/opt/vision/.env`, owned by `vision`, mode `600`. This is read by both
the systemd units (`EnvironmentFile=`) and Compose (`env_file:`).

```bash
sudo -u vision install -m 600 /dev/null /opt/vision/.env
sudoedit /opt/vision/.env
```

Minimum production values (see `src/vision/config.py` for the full schema):

```dotenv
VISION_ENV=live                         # dry_run -> staging -> live
DATABASE_URL=postgresql+psycopg://vision:<secret>@localhost:5432/vision
SECRET_HMAC_KEY=<32+ random bytes>      # signs approval tokens — MUST override dev default
TOKEN_ENC_KEY=<32+ random bytes>        # encrypts OAuth tokens at rest
LI_CLIENT_ID=<linkedin app id>
LI_CLIENT_SECRET=<linkedin app secret>
LI_REDIRECT_URI=https://<your-domain>/oauth/linkedin/callback
EMAIL_PROVIDER=smtp
EMAIL_FROM=vision@<your-domain>
EMAIL_TO=vishnu.wildeagle@gmail.com
EMAIL_API_KEY=<provider key>
# Compose Postgres service also reads these:
POSTGRES_DB=vision
POSTGRES_USER=vision
POSTGRES_PASSWORD=<secret>
```

Generate strong keys with `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
**Never** paste secrets into a terminal that logs, or commit `.env`. `/healthz`
reports `token_secret: insecure-default` until `SECRET_HMAC_KEY` is overridden —
use that as a go/no-go check.

---

## 3. Install & enable systemd units

```bash
# Copy service + timer units into place.
sudo cp /opt/vision/repo/deploy/systemd/vision-*.service /etc/systemd/system/
sudo cp /opt/vision/repo/deploy/systemd/vision-*.timer   /etc/systemd/system/
sudo systemctl daemon-reload

# Always-on web service.
sudo systemctl enable --now vision-web.service

# Scheduled jobs (timers, not the services directly).
sudo systemctl enable --now vision-daily.timer vision-publisher.timer \
                            vision-token.timer vision-canary.timer

# Confirm.
systemctl status vision-web.service --no-pager | head -5
systemctl list-timers --no-pager | grep vision
```

Nightly backup: add the `backup.py` line from `deploy/crontab.example` to the
`vision` user's crontab (`sudo crontab -u vision -e`), or wrap it in a matching
`vision-backup.service`/`.timer` if you prefer everything under systemd.

---

## 4. TLS via the existing reverse proxy

`vision-web` listens on `127.0.0.1:8000` (HTTP, loopback only). Terminate TLS at
the existing nginx/Traefik and proxy to it. Only the signed approval routes need
to be reachable. Example nginx location:

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto https;
    # Do NOT forward Referer — the app already sets Referrer-Policy: no-referrer,
    # but keep the token out of upstream logs too.
    proxy_hide_header Referer;
}
```

Rate-limiting at the proxy is defence-in-depth on top of the app's per-IP /
per-token limits (threat model §2). Restrict `/healthz` to internal monitors if
you don't want it publicly visible.

---

## 5. Docker Compose path (alternative)

```bash
# Build the image and start the always-on web + postgres.
docker compose --env-file .env up -d --build vision-web postgres

# Run a one-shot job on demand (or from host cron per deploy/crontab.example):
docker compose --env-file .env run --rm vision-daily
docker compose --env-file .env run --rm vision-publisher
docker compose --env-file .env run --rm vision-token
docker compose --env-file .env run --rm vision-canary
```

Memory caps (`mem_limit`) are set on every service to honour the prior VPS
memory-overload incident — do not remove them.

---

## 6. Deploy updates

Pull + reinstall + restart is scripted with is-active safety checks:

```bash
sudo /opt/vision/repo/deploy/deploy.sh
```

It runs `git pull --ff-only`, `pip install -e .`, `systemctl daemon-reload`,
restarts only currently-active services/timers, then verifies `/healthz`
returns 200.

---

## 7. Backup & restore

```bash
# Manual backup (also runs nightly at 02:00 IST).
sudo -u vision /opt/vision/venv/bin/python /opt/vision/repo/scripts/backup.py

# Restore a Postgres custom-format dump into the vision schema.
pg_restore --clean --if-exists --no-owner \
  --dbname="postgresql://vision:<secret>@localhost:5432/vision" \
  /opt/vision/backups/vision_backup_<stamp>.dump
```

Backups retain the newest 14 (one/night ≈ 14 days). Store them off-box and
encrypted for real disaster recovery.

---

## 8. Health, logs, alerts

```bash
# Liveness.
curl -s http://127.0.0.1:8000/healthz | python -m json.tool

# Logs (per-job append files).
tail -f /opt/vision/logs/vision-web.log
tail -f /opt/vision/logs/vision-daily.log
tail -f /opt/vision/logs/vision-canary.log
```

The canary emails the owner on a failed probe (outside `dry_run`); the publisher
alerts on stuck drafts; the token job exits non-zero when a re-auth is needed.
Watch for the ~yearly LinkedIn re-authorisation alert.

---

## 9. Go-live checklist

- [ ] `.env` present, mode `600`, `SECRET_HMAC_KEY` / `TOKEN_ENC_KEY` overridden.
- [ ] `/healthz` returns `status: ok` and `token_secret: configured`.
- [ ] Migrations applied (`alembic current` at head).
- [ ] Timers listed in `systemctl list-timers`.
- [ ] Reverse proxy serves the approval URL over HTTPS; loopback port not public.
- [ ] One STAGING dry run (`VISION_ENV=staging`) posts-then-deletes cleanly.
- [ ] Nightly backup produced a file; a test `pg_restore` succeeded.
- [ ] `mem_limit` / `MemoryMax` caps in place (prior memory incident).

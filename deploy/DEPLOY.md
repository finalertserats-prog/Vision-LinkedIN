# VISION — Linux VPS deployment

The generation pipeline shells out to the **Brahmastra CLIs** (`agy`, `codex`,
`claude`). On this VPS you have already confirmed Brahmastra works, so the hard
part (headless CLI auth) is done. What remains is platform glue: Linux paths,
systemd scheduling, and making the approval links reachable.

Everything runs as the **`vision`** service user under **`/opt/vision`**.

---

## 0. What runs (systemd units in `deploy/systemd/`)

| Unit | Type | Cadence | Purpose |
|------|------|---------|---------|
| `vision-web.service` | long-running | always | Approval web server (email links) |
| `vision-council.timer` | oneshot | daily 08:00 IST | **Primary** content: deliberate → post + anime → email |
| `vision-publisher.timer` | oneshot | every 5 min | Publish approved & due drafts (with image) |
| `vision-expire.timer` | oneshot | daily 20:00 IST | Fail-closed expiry of un-actioned drafts |
| `vision-retention.timer` | oneshot | Sun 03:30 IST | Archive → Google Drive (rclone) → prune |
| `vision-token` / `vision-canary` | oneshot | as configured | Token refresh / external health canary |
| `vision-daily.timer` | oneshot | *(disabled)* | Older news-mode lane — enable only if you also want news posts |

---

## 1. Prerequisites (once)

- **Python 3.11**, `git`, and the repo cloned to `/opt/vision`.
- **Brahmastra CLIs authenticated *for the `vision` user*.** The units run as
  `vision`, so `agy`/`codex`/`claude` must be logged in under **`/home/vision`**
  (`~/.claude/.credentials.json`, `~/.codex/auth.json`, `~/.gemini/oauth_creds.json`).
  If you set Brahmastra up under a different user, either re-auth as `vision` or
  change `User=`/`Group=` in the units to that user.
- Optional: **`rclone`** (for Drive backups), **PostgreSQL** (else SQLite is fine).

> **Why the council/retention units use `ProtectSystem=full` and not `strict`/`ProtectHome`:**
> the CLIs and rclone **refresh their OAuth tokens in `$HOME`**. Stricter hardening
> makes `$HOME` read-only or invisible and silently kills the generation/backup
> lanes. `full` keeps `/opt` + `$HOME` writable while still locking `/usr`, `/etc`.

---

## 2. One-time setup

```bash
sudo useradd -r -m -d /home/vision -s /bin/bash vision      # if not present
sudo mkdir -p /opt/vision && sudo chown -R vision:vision /opt/vision
sudo -u vision git clone <repo> /opt/vision
cd /opt/vision
sudo -u vision python3.11 -m venv venv
sudo -u vision venv/bin/pip install -e .
sudo -u vision mkdir -p logs
```

Create `/opt/vision/.env` (copy your working `.env`, then fix the **Linux-specific**
keys):

```ini
VISION_ENV=live
# LINUX agy path (NOT the Windows .exe). Find it: sudo -u vision which agy
AGY_BIN=/home/vision/.local/bin/agy
# The public URL the approval email links point at (see §4). MUST be reachable
# from your phone/browser or you can't approve.
VISION_APPROVAL_BASE_URL=https://vision.yourdomain.com
# SQLite (default) or Postgres:
DATABASE_URL=sqlite:////opt/vision/vision.db
# Keep your existing secrets: SECRET_HMAC_KEY, TOKEN_ENC_KEY, EMAIL_*, LI_*, etc.
# Retention Drive backup (optional, §5):
# RCLONE_REMOTE=gdrive
```

> `.env` is loaded by the app from an **absolute path**, so it is found no matter
> what working directory systemd uses. Keep it `chmod 600`, owned by `vision`.

---

## 3. Preflight — prove it before arming anything

```bash
cd /opt/vision && sudo -u vision deploy/preflight.sh
```

Green across the board, then run ONE real end-to-end pass and check your inbox:

```bash
sudo -u vision venv/bin/vision-council      # deliberates → emails you a draft
```

If that email arrives with a natural-voice post + anime image, the whole lane works.

---

## 4. Arm the services (first time)

```bash
sudo install -m 0644 deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vision-web.service
sudo systemctl enable --now vision-council.timer vision-publisher.timer \
                              vision-expire.timer vision-retention.timer
# (enable vision-token/vision-canary/vision-daily only if you use them)
systemctl list-timers 'vision-*'
```

**Make the approval links reachable.** The web binds to `127.0.0.1:8000`. Put a
reverse proxy in front and point `VISION_APPROVAL_BASE_URL` at its HTTPS URL:

```nginx
server {
    server_name vision.yourdomain.com;
    location / { proxy_pass http://127.0.0.1:8000; proxy_set_header Host $host; }
    # add TLS via certbot
}
```

Simplest secure alternative: a **Tailscale** address, or an SSH tunnel when you
want to approve. Whatever you choose, the URL in `VISION_APPROVAL_BASE_URL` must
be what your browser can open.

---

## 5. Google Drive backups (optional)

```bash
sudo -u vision rclone config            # new remote "gdrive", type drive, OAuth
# then in .env:  RCLONE_REMOTE=gdrive
```

Until this is set, retention safely archives locally and **skips pruning** (never
deletes un-backed-up data).

---

## 6. Ongoing deploys

```bash
sudo /opt/vision/deploy/deploy.sh       # git pull → pip install -e . → reload → healthz
```

`deploy.sh` reinstalls units, reloads systemd, actively arms the fail-closed expiry
timer, restarts the (already-active) web + timers, and fails the deploy if
`/healthz` isn't 200 afterwards.

---

## 7. Monitor

```bash
systemctl status vision-web.service
systemctl list-timers 'vision-*'
journalctl --user -u vision-council.service -n 50      # or: tail -f logs/vision-council.log
curl -s localhost:8000/healthz
```

Every job logs to `/opt/vision/logs/<unit>.log` and exits non-zero on failure, so
`systemctl --failed` and the logs are your first stop.

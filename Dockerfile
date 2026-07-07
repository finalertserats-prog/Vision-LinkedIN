# ---------------------------------------------------------------------------
# Project VISION — production container image (BRD §19).
#
# WHY this shape:
#   * python:3.11-slim — matches `requires-python >=3.11` and keeps the image
#     small; every runtime dependency (pillow / matplotlib / cryptography) ships
#     a manylinux wheel for cp311, so NO compiler toolchain is needed and we can
#     stay on -slim instead of pulling build-essential.
#   * One image, many roles — the same image runs the always-on web service AND
#     every one-shot cron job (daily / publisher / token / canary). The role is
#     chosen at runtime via the VISION_ROLE env var (config over code, §22), so
#     compose/systemd never need role-specific images.
#   * Non-root — the threat model (prep/security_threatmodel.md §2) mandates the
#     service run unprivileged; a stolen web process must not own the host.
#   * Secrets are NEVER baked in (§19). The .env is injected at runtime by
#     compose/systemd; this image is safe to store in a registry.
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

# Fail fast and keep Python well-behaved inside a container:
#   PYTHONDONTWRITEBYTECODE — no .pyc clutter on the read-only-ish layer.
#   PYTHONUNBUFFERED       — logs stream immediately (systemd/compose capture).
#   PIP_NO_CACHE_DIR       — smaller image, no wheel cache left behind.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# postgresql-client provides `pg_dump` for scripts/backup.py; it is the only OS
# package we add. curl is intentionally omitted — the healthcheck/canary use the
# Python stdlib so the attack surface stays minimal.
RUN apt-get update \
    && apt-get install --no-install-recommends -y postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Create an unprivileged user up front so every subsequent COPY/RUN can be owned
# by it. A fixed UID/GID keeps host bind-mount permissions predictable.
RUN groupadd --gid 10001 vision \
    && useradd --uid 10001 --gid vision --create-home --home-dir /home/vision vision

WORKDIR /opt/vision

# Copy only what pip needs to install the package. Copying the whole tree in one
# layer is fine here (small pure-Python repo); src/ + metadata are the inputs.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY scripts ./scripts

# Install the package itself (not editable — the image is immutable; editable
# installs are for the VPS working copy via deploy/deploy.sh). This wires the
# console entry points (vision-daily / vision-publisher / vision-token /
# vision-expire) declared in pyproject [project.scripts].
RUN pip install . && pip check

# ---------------------------------------------------------------------------
# Role dispatcher. A tiny, fully-commented shell entrypoint selects the process
# to exec based on $VISION_ROLE (default: web). Kept inline here rather than as a
# separate tracked script so the whole build contract lives in one file.
# ---------------------------------------------------------------------------
RUN cat > /usr/local/bin/vision-entrypoint <<'ENTRYPOINT' \
    && chmod +x /usr/local/bin/vision-entrypoint
#!/bin/sh
# Fail-closed shell: -e aborts on any error, -u treats unset vars as errors.
set -eu

# Role precedence: explicit $VISION_ROLE wins; else first CLI arg; else "web".
ROLE="${VISION_ROLE:-${1:-web}}"

case "$ROLE" in
  web)
    # Always-on FastAPI approval service. --factory lets uvicorn call
    # create_app() (web.py deliberately exposes no module-level app). Bind to
    # 0.0.0.0 INSIDE the container; the host reverse proxy terminates TLS and
    # is the only thing that should reach this port (threat model §2).
    exec uvicorn --factory vision.approval.web:create_app \
      --host "${VISION_WEB_HOST:-0.0.0.0}" \
      --port "${VISION_WEB_PORT:-8000}"
    ;;
  daily)      exec vision-daily ;;      # 06:30 IST ingest→synthesise→email
  publisher)  exec vision-publisher ;;  # every ~5 min publish approved+due
  token)      exec vision-token ;;      # 02:00 IST proactive OAuth refresh
  expire)     exec vision-expire ;;     # 20:00 IST auto-expire un-actioned
  canary)
    # Liveness canary (vision.ops.canary): probes the web tier's /healthz and
    # exits non-zero on anything but HTTP 200 (fail-closed). The probe target is
    # set via VISION_HEALTHZ_URL (compose points it at http://vision-web:8000).
    exec vision-canary ;;
  *)
    echo "vision-entrypoint: unknown VISION_ROLE '$ROLE'" >&2
    exit 64  # EX_USAGE — misconfiguration, not a transient fault
    ;;
esac
ENTRYPOINT

# Drop privileges for everything that runs from here on.
USER vision

# Default role is the web service; compose/systemd override VISION_ROLE per job.
ENV VISION_ROLE=web
ENTRYPOINT ["/usr/local/bin/vision-entrypoint"]

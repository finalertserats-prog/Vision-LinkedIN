#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Project VISION — VPS deploy script (BRD §19). Adapted from finalert/deploy.sh.
#
# Flow: git pull → pip install -e . → reload systemd → restart services, each
# step guarded so a failure aborts cleanly (fail-closed, §22) instead of leaving
# a half-deployed box. Run as the deploy operator; the services run as `vision`.
#
# Usage:  sudo /opt/vision/deploy/deploy.sh
# ---------------------------------------------------------------------------
set -euo pipefail

# Root of the working copy on the VPS. Overridable for non-standard layouts.
VISION_HOME="${VISION_HOME:-/opt/vision}"
VENV_PY="${VISION_HOME}/venv/bin/python"
BRANCH="${VISION_BRANCH:-main}"

# The always-on service (restarted every deploy) and the timer units (only need
# a daemon-reload so the next scheduled tick picks up new unit files/code).
WEB_SERVICE="vision-web.service"
# vision-council = primary content generator; vision-retention = weekly Drive backup
# + prune (both added 2026-07-08). vision-daily is the older news-mode lane — leave
# its timer disabled unless you also want news posts. deploy.sh only *restarts*
# already-active timers, so listing one the operator hasn't enabled is a safe no-op.
TIMERS=(vision-council.timer vision-daily.timer vision-publisher.timer vision-expire.timer vision-retention.timer vision-token.timer vision-canary.timer)

echo "=== VISION deploy: pulling ${BRANCH} in ${VISION_HOME} ==="
git -C "${VISION_HOME}" pull --ff-only origin "${BRANCH}"

echo "=== Installing package (editable) + pinned deps ==="
# Editable install so the systemd ExecStart entry points always resolve to the
# freshly pulled code without reinstalling on every run.
"${VENV_PY}" -m pip install -e "${VISION_HOME}" -q
"${VENV_PY}" -m pip check

echo "=== Installing systemd unit files ==="
# Copy the repo's unit files into the system dir FIRST — a bare daemon-reload only
# re-reads already-installed units, so a new/changed unit (e.g. the fail-closed
# vision-expire.timer) would otherwise never land while the deploy still reports
# success. Installing here keeps the units authoritative from the working copy.
SYSTEMD_SRC="${VISION_HOME}/deploy/systemd"
SYSTEMD_DEST="/etc/systemd/system"
install -m 0644 "${SYSTEMD_SRC}"/*.service "${SYSTEMD_SRC}"/*.timer "${SYSTEMD_DEST}/"

echo "=== Reloading systemd unit definitions ==="
# Picks up the .service/.timer files just copied into /etc/systemd/system.
systemctl daemon-reload

echo "=== Ensuring the fail-closed expiry timer is armed (BRD §22.9) ==="
# The 20:00 IST expiry is NOT optional: if it is disabled or absent, un-actioned
# drafts are never expired and could later be posted. Unlike the other timers
# (restarted only when already active), we ACTIVELY enable+start this one so a
# first deploy — or an accidentally-disabled timer — is corrected, not skipped.
# `set -e` means a failure to arm it aborts the deploy (fail-closed).
systemctl enable --now vision-expire.timer
echo "    vision-expire.timer enabled and started"

echo "=== Restarting always-on web service (only if already active) ==="
# is-active guard: never *start* a service the operator had deliberately stopped;
# only restart what is currently running (matches finalert's safety check).
if systemctl is-active --quiet "${WEB_SERVICE}"; then
  systemctl restart "${WEB_SERVICE}"
  echo "    ${WEB_SERVICE} restarted"
else
  echo "    ${WEB_SERVICE} is not active — skipping (start it manually if intended)"
fi

echo "=== Ensuring timers are loaded (restart only the active ones) ==="
for timer in "${TIMERS[@]}"; do
  if systemctl is-active --quiet "${timer}"; then
    systemctl restart "${timer}"
    echo "    ${timer} reloaded"
  else
    echo "    ${timer} is not active — skipping"
  fi
done

echo "=== Post-deploy health check ==="
# Give uvicorn a moment to rebind, then confirm the web tier answers /healthz.
# A non-200 (or unreachable) here means the deploy left the service unhealthy.
# Fail-closed (§22.9): a broken deploy MUST report failure, never SUCCESS — so we
# exit non-zero. The is-active guard below only restarts a service that was
# already running, matching the deploy's "never start what was deliberately
# stopped" rule; the non-zero exit still surfaces the failure to CI/the operator.
sleep 3
if "${VENV_PY}" -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=5).status==200 else 1)"; then
  echo "    /healthz OK"
else
  echo "    ERROR: /healthz did not return 200 — deploy left the web tier unhealthy." >&2
  echo "    Attempting one restart of ${WEB_SERVICE} before failing the deploy..." >&2
  # Best-effort self-heal: restart only if the unit is already active, then re-probe.
  if systemctl is-active --quiet "${WEB_SERVICE}"; then
    systemctl restart "${WEB_SERVICE}" || true
    sleep 3
    if "${VENV_PY}" -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=5).status==200 else 1)"; then
      echo "    /healthz recovered after restart"
    else
      echo "    FATAL: /healthz still failing after restart — inspect logs/vision-web.log" >&2
      exit 1
    fi
  else
    echo "    FATAL: ${WEB_SERVICE} is not active and /healthz is failing — inspect logs/vision-web.log" >&2
    exit 1
  fi
fi

echo "=== Deployed $(date -u '+%Y-%m-%dT%H:%M:%SZ') ==="
git -C "${VISION_HOME}" log --oneline -3

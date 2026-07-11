#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# VISION VPS preflight — prove the box is ready BEFORE arming the timers.
# Run as the SERVICE user (the one systemd units use, e.g. `vision`), from the
# project root, so it checks the exact environment the units will run in.
#
#   cd /opt/vision && sudo -u vision deploy/preflight.sh
#
# Exits non-zero on the first hard failure. Warnings (optional lanes) don't fail.
# ---------------------------------------------------------------------------
set -uo pipefail

ROOT="${VISION_HOME:-$(cd "$(dirname "$0")/.." && pwd)}"
PY="${ROOT}/venv/bin/python"
[ -x "$PY" ] || PY="${ROOT}/.venv/bin/python"
fail=0
ok()   { printf '  \033[32mOK\033[0m   %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; fail=1; }
warn() { printf '  \033[33mWARN\033[0m %s\n' "$1"; }

echo "VISION preflight — root=${ROOT}"

# 1) venv + console scripts resolve.
if [ -x "$PY" ]; then ok "python venv: $PY"; else bad "no venv python at $PY (create it + pip install -e .)"; fi
for s in vision-council vision-publisher vision-expire vision-retention; do
  if [ -x "$(dirname "$PY")/$s" ]; then ok "console script: $s"; else bad "missing console script $s (pip install -e .)"; fi
done

# 2) config loads and is LIVE with credentials (never prints secret values).
"$PY" - <<'PYEOF' && ok "config loads: VISION_ENV=live, token secret configured" || bad "config not live / missing secrets (check .env)"
from vision.config import get_settings
s = get_settings()
assert s.vision_env.value == "live", f"VISION_ENV={s.vision_env.value} (want live)"
assert s.secret_hmac_key and s.token_enc_key, "SECRET_HMAC_KEY / TOKEN_ENC_KEY missing"
assert s.email_api_key, "EMAIL_API_KEY missing"
raise SystemExit(0)
PYEOF

# 3) agy binary configured for THIS host (must be the Linux path, not a .exe).
"$PY" - <<'PYEOF' && ok "AGY_BIN points at an existing binary" || warn "AGY_BIN not found — image gen will degrade to text-only (set the Linux agy path in .env)"
import os, sys
from vision.config import get_settings
b = get_settings().agy_bin
sys.exit(0 if b and os.path.exists(b) else 1)
PYEOF

# 4) bundled ffmpeg (video lane) resolves.
"$PY" -c "import imageio_ffmpeg,os,sys; sys.exit(0 if os.path.exists(imageio_ffmpeg.get_ffmpeg_exe()) else 1)" 2>/dev/null \
  && ok "ffmpeg (imageio-ffmpeg) available" || warn "imageio-ffmpeg missing (video lane only)"

# 5) DB is reachable + writable.
"$PY" -c "from vision.db.session import SessionLocal; from sqlalchemy import text; s=SessionLocal(); s.execute(text('SELECT 1')); s.close()" 2>/dev/null \
  && ok "database reachable" || bad "database not reachable (DATABASE_URL / permissions)"

# 6) logs dir writable (systemd units append here).
if [ -d "${ROOT}/logs" ] && [ -w "${ROOT}/logs" ]; then ok "logs dir writable"; else warn "create a writable ${ROOT}/logs (mkdir -p logs)"; fi

# 7) Brahmastra CLI auth reachable — a fast claude -p ping (agy/codex use the same
#    cached-token model; a full 'vision-council' run is the real end-to-end proof).
if command -v claude >/dev/null 2>&1; then
  if timeout 60 claude -p "reply with exactly: PREFLIGHT_OK" </dev/null 2>/dev/null | grep -q PREFLIGHT_OK; then
    ok "claude CLI authenticated + headless"
  else
    bad "claude CLI did not respond headless (auth for THIS user? ~/.claude/.credentials.json)"
  fi
else
  warn "claude not on PATH for this user"
fi

# 8) mermaid CLI (mmdc) — needed for the tech-post DIAGRAM lane. Optional: a
#    missing mmdc degrades a technical post to its anime concept illustration
#    (never a crash), but the in-sync diagram will not render without it.
if command -v mmdc >/dev/null 2>&1; then
  ok "mermaid CLI (mmdc) present — diagram lane will render"
else
  warn "mmdc not found — tech posts fall back to concept art (npm i -g @mermaid-js/mermaid-cli)"
fi

echo ""
if [ "$fail" -eq 0 ]; then
  echo "PREFLIGHT PASSED. Next: run one real end-to-end council pass, then arm the timers (see DEPLOY.md)."
  echo "  ${ROOT}/venv/bin/vision-council   # produces a draft + emails you"
  exit 0
else
  echo "PREFLIGHT FAILED — fix the FAIL lines above before arming timers."
  exit 1
fi

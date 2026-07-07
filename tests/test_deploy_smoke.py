"""Smoke tests for the deployment artefacts (BRD §19 / Phase 4).

These are *static* checks — they never launch Docker, open a socket, or shell out
to a real ``pg_dump``. They assert that the files an operator relies on are
well-formed and encode the guardrails the BRD demands:

  * the Dockerfile builds a slim, non-root, role-dispatching image;
  * docker-compose declares the expected services, a web healthcheck, and a
    memory cap on every service (prior VPS memory-overload incident);
  * ``scripts/backup.py``'s retention logic keeps exactly the newest N backups;
  * every scheduled line in ``crontab.example`` is a well-formed 5-field entry.

Each test follows Arrange → Act → Assert.
"""

from __future__ import annotations

import importlib.util
import os
import re
from pathlib import Path
from types import ModuleType

import yaml

# Repo root: tests/ lives directly under it.
ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
COMPOSE_FILE = ROOT / "docker-compose.yml"
CRONTAB_FILE = ROOT / "deploy" / "crontab.example"
BACKUP_SCRIPT = ROOT / "scripts" / "backup.py"
SYSTEMD_DIR = ROOT / "deploy" / "systemd"
DEPLOY_SCRIPT = ROOT / "deploy" / "deploy.sh"

# The services the compose topology must define (BRD §19).
# vision-expire is the fail-closed cutoff job (§22.9): un-actioned drafts MUST be
# auto-expired so they are never posted. It has to exist under BOTH deploy shapes
# (systemd timers AND compose), or the fail-closed guarantee silently breaks.
_EXPECTED_SERVICES = frozenset(
    {
        "vision-web",
        "postgres",
        "vision-daily",
        "vision-publisher",
        "vision-token",
        "vision-canary",
        "vision-expire",
    }
)

# Every scheduled (timer-driven) role that must ship BOTH a .service and a .timer
# under deploy/systemd/. vision-expire was previously missing → drafts never
# expired under the systemd deploy shape.
_EXPECTED_TIMER_ROLES = ("vision-daily", "vision-publisher", "vision-token", "vision-canary", "vision-expire")

# A cron time field is '*' or digits combined with , - / * (e.g. '*/5', '0', '1-5').
_CRON_FIELD = re.compile(r"^[\d\*/,\-]+$")


def _load_backup_module() -> ModuleType:
    """Import ``scripts/backup.py`` by path (it is a script, not a package).

    ``vision`` is importable via the src/ pythonpath, so the module's top-level
    imports resolve; loading by path avoids adding scripts/ to sys.path.
    """
    spec = importlib.util.spec_from_file_location("vision_backup_under_test", BACKUP_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Dockerfile
# ---------------------------------------------------------------------------
def test_dockerfile_is_slim_nonroot_and_role_dispatching() -> None:
    # Arrange: read the Dockerfile as text.
    text = DOCKERFILE.read_text(encoding="utf-8")

    # Act: (no action — static assertions on the content below).

    # Assert: slim 3.11 base, an unprivileged USER, and the role dispatcher.
    assert "FROM python:3.11-slim" in text
    assert "USER vision" in text  # non-root runtime (threat model §2)
    assert "VISION_ROLE" in text  # config-over-code role selection
    assert "ENTRYPOINT" in text
    # Every runtime role must be reachable from the dispatcher.
    for role in ("web", "daily", "publisher", "token", "canary"):
        assert f"{role})" in text, f"dispatcher missing role: {role}"


# ---------------------------------------------------------------------------
# docker-compose.yml
# ---------------------------------------------------------------------------
def test_compose_parses_and_declares_expected_services() -> None:
    # Arrange + Act: parse the compose file (basic lint = it must be valid YAML).
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))

    # Assert: the services block exists and contains every expected service.
    services = compose["services"]
    assert _EXPECTED_SERVICES.issubset(services.keys())


def test_compose_web_has_healthcheck() -> None:
    # Arrange + Act.
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))

    # Assert: the always-on web service defines a healthcheck (BRD §19).
    assert "healthcheck" in compose["services"]["vision-web"]


def test_compose_every_service_has_a_memory_limit() -> None:
    # Arrange + Act.
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))

    # Assert: every service caps memory — honours the prior VPS OOM incident.
    for name, spec in compose["services"].items():
        assert "mem_limit" in spec, f"service {name} is missing mem_limit"


# ---------------------------------------------------------------------------
# scripts/backup.py — retention logic (mock filesystem via tmp_path)
# ---------------------------------------------------------------------------
def test_prune_keeps_only_the_newest_n_backups(tmp_path: Path) -> None:
    # Arrange: 20 fake backups with strictly increasing mtimes so ordering is
    # deterministic; higher index == newer.
    backup = _load_backup_module()
    total, keep = 20, 14
    created: list[Path] = []
    for index in range(total):
        f = tmp_path / f"vision_backup_2026010{index:02d}.dump"
        f.write_text("dump", encoding="utf-8")
        # Space mtimes 60s apart, oldest first.
        os.utime(f, (1_700_000_000 + index * 60, 1_700_000_000 + index * 60))
        created.append(f)

    # Act: prune down to the newest `keep`.
    deleted = backup.prune_old_backups(tmp_path, keep=keep)

    # Assert: exactly `keep` survive, and the survivors are the newest ones.
    remaining = sorted(tmp_path.glob("vision_backup_*"))
    assert len(remaining) == keep
    assert len(deleted) == total - keep
    # The 6 oldest (indices 0..5) were deleted; the newest 14 remain.
    assert set(deleted) == set(created[: total - keep])


def test_prune_is_a_noop_when_under_the_limit(tmp_path: Path) -> None:
    # Arrange: fewer files than the retention count.
    backup = _load_backup_module()
    for index in range(3):
        (tmp_path / f"vision_backup_x{index}.dump").write_text("d", encoding="utf-8")

    # Act.
    deleted = backup.prune_old_backups(tmp_path, keep=14)

    # Assert: nothing deleted when we are under the ceiling.
    assert deleted == []
    assert len(list(tmp_path.glob("vision_backup_*"))) == 3


# ---------------------------------------------------------------------------
# deploy/crontab.example — every schedule line is well-formed
# ---------------------------------------------------------------------------
def test_crontab_lines_are_well_formed() -> None:
    # Arrange: read the example crontab.
    lines = CRONTAB_FILE.read_text(encoding="utf-8").splitlines()

    # Act: keep only real schedule rows (drop blanks, comments, VAR= assignments).
    schedule_lines = [
        ln
        for ln in lines
        if ln.strip()
        and not ln.lstrip().startswith("#")
        and "=" not in ln.split()[0]  # skip MAILTO=/PATH= env assignments
    ]

    # Assert: we found the expected jobs, and each has 5 valid time fields + cmd.
    assert len(schedule_lines) >= 5  # daily, publisher, expire, token, backup
    for ln in schedule_lines:
        fields = ln.split()
        # 5 time fields + at least one command token.
        assert len(fields) >= 6, f"too few fields: {ln!r}"
        for time_field in fields[:5]:
            assert _CRON_FIELD.match(time_field), f"bad cron field {time_field!r} in {ln!r}"


# ---------------------------------------------------------------------------
# deploy/systemd — the fail-closed expiry job must exist under systemd too
# ---------------------------------------------------------------------------
def test_every_scheduled_role_ships_a_service_and_timer() -> None:
    # Arrange + Act: enumerate the unit files actually present on disk.
    present = {p.name for p in SYSTEMD_DIR.iterdir()}

    # Assert: each timer-driven role ships BOTH halves. A missing vision-expire
    # unit means un-actioned drafts are never expired under the systemd deploy
    # shape — a silent breach of the fail-closed cutoff (BRD §22.9).
    for role in _EXPECTED_TIMER_ROLES:
        assert f"{role}.service" in present, f"missing systemd unit: {role}.service"
        assert f"{role}.timer" in present, f"missing systemd timer: {role}.timer"


def test_expire_timer_fires_at_2000_ist_and_catches_up() -> None:
    # Arrange: the expire cutoff must run at 20:00 IST and be Persistent so a
    # host that was down at 20:00 still expires stale drafts on next boot
    # (otherwise a draft could survive past its cutoff and later be posted).
    timer_text = (SYSTEMD_DIR / "vision-expire.timer").read_text(encoding="utf-8")

    # Assert: correct calendar slot, timezone, catch-up, and wiring.
    assert "20:00:00 Asia/Kolkata" in timer_text
    assert "Persistent=true" in timer_text
    assert "Unit=vision-expire.service" in timer_text


def test_expire_service_is_oneshot_and_hardened() -> None:
    # Arrange: the expire job is a one-shot cron-style unit like the token job.
    service_text = (SYSTEMD_DIR / "vision-expire.service").read_text(encoding="utf-8")

    # Assert: it mirrors the hardened token unit (oneshot, non-root, bounded).
    assert "Type=oneshot" in service_text
    assert "User=vision" in service_text
    assert "ExecStart=/opt/vision/venv/bin/vision-expire" in service_text
    assert "MemoryMax=" in service_text
    assert "NoNewPrivileges=true" in service_text


# ---------------------------------------------------------------------------
# deploy/systemd — restart-storm guard must actually be honoured by systemd
# ---------------------------------------------------------------------------
def test_web_start_limit_directives_live_under_unit_section() -> None:
    # Arrange: systemd only honours StartLimitIntervalSec / StartLimitBurst in the
    # [Unit] section. Placed under [Service] they are silently ignored and the
    # web tier crash-loops forever every RestartSec — the guard is a no-op.
    text = (SYSTEMD_DIR / "vision-web.service").read_text(encoding="utf-8")

    # Act: split the ini into its sections so we can check membership precisely.
    sections: dict[str, list[str]] = {}
    current = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections[current] = []
        elif current:
            sections[current].append(line)

    unit_body = "\n".join(sections.get("Unit", []))
    service_body = "\n".join(sections.get("Service", []))

    # Assert: the restart-storm directives are under [Unit], not [Service].
    assert "StartLimitIntervalSec=" in unit_body, "StartLimitIntervalSec must be in [Unit]"
    assert "StartLimitBurst=" in unit_body, "StartLimitBurst must be in [Unit]"
    assert "StartLimitIntervalSec=" not in service_body
    assert "StartLimitBurst=" not in service_body
    # RestartSec stays in [Service] — that IS where systemd honours it.
    assert "RestartSec=" in service_body


# ---------------------------------------------------------------------------
# deploy/deploy.sh — a failed post-deploy health check must fail the deploy
# ---------------------------------------------------------------------------
def test_deploy_script_exits_nonzero_when_healthz_fails() -> None:
    # Arrange: a broken deploy must NOT report success. The health-check branch
    # has to exit non-zero (fail-closed, §22.9) so CI/operators see the failure.
    # Static assertion: the non-200 branch exits with a non-zero status rather
    # than merely warning-and-continuing.
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    # Assert: an `exit 1` (or any non-zero exit) guards the health-check failure.
    # We look for a health-check failure path that terminates the script.
    assert re.search(r"exit\s+[1-9]", text), "deploy.sh must exit non-zero on health-check failure"
    # And the success path is still logged (no accidental removal of the OK log).
    assert "/healthz OK" in text


def test_deploy_script_installs_repo_systemd_units() -> None:
    # Arrange: a new/changed unit (e.g. vision-expire.timer) only takes effect if
    # deploy copies deploy/systemd/* into /etc/systemd/system BEFORE daemon-reload.
    # Otherwise daemon-reload just re-reads stale/absent units and the fail-closed
    # expiry timer never lands, while the deploy still reports success (Codex HIGH).
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    # Assert: units are synced into the system dir, and it happens before reload.
    assert "/etc/systemd/system" in text, "deploy.sh must install units into /etc/systemd/system"
    install_pos = text.find("install -m")
    reload_pos = text.find("systemctl daemon-reload")
    assert install_pos != -1, "deploy.sh must copy unit files with install(1)"
    assert reload_pos != -1
    assert install_pos < reload_pos, "unit install must precede daemon-reload"


def test_deploy_script_enables_fail_closed_expire_timer() -> None:
    # Arrange: the expire timer is NOT optional — if it is disabled/absent the
    # fail-closed cutoff (§22.9) silently never fires. The deploy must actively
    # enable+start it (not merely "restart if already active"), so a first deploy
    # or an accidentally-disabled timer is corrected rather than skipped.
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    # Assert: the expire timer is enabled-and-started as a hard requirement.
    assert re.search(r"enable\b.*vision-expire\.timer", text), (
        "deploy.sh must enable the fail-closed vision-expire.timer"
    )


# ---------------------------------------------------------------------------
# deploy/systemd — no phantom watchdog on a non-sd_notify web service
# ---------------------------------------------------------------------------
def test_web_service_has_no_watchdog_without_sd_notify() -> None:
    # Arrange: vision-web is Type=simple and uvicorn never sends sd_notify
    # WATCHDOG pings. A WatchdogSec here risks systemd killing/restarting a
    # perfectly healthy web tier on a fixed interval (the finalert outage that
    # needed a watchdog-disable drop-in). Restart=always is the real liveness net.
    text = (SYSTEMD_DIR / "vision-web.service").read_text(encoding="utf-8")

    # Assert: no watchdog directive, but the active restart guard is intact.
    assert "WatchdogSec" not in text, "remove WatchdogSec — uvicorn does not sd_notify"
    assert "Restart=always" in text

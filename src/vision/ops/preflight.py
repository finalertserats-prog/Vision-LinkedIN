"""Lane preflight — ask Brahmastra if the AI lanes are alive before running (§17).

WHY this module exists (incident 2026-08-06): the daily council spent two minutes
deliberating against a Claude lane whose OAuth token had expired, then died in the
composer. Brahmastra was installed on the very same box, with a mesh health
checker, and nobody asked it. (It would have lied anyway — it hardcoded the Claude
lane as healthy without probing it, fixed alongside this in
``godmode-mesh-health.sh``.)

This is the "ask first" step. It runs Brahmastra's own health script and reads the
``mesh-status.json`` it writes, then applies one policy:

* **Any dead lane alerts.** A two-voice council is a degraded council and the
  owner should know the post was thinner than usual.
* **Only a dead Claude blocks.** Claude is the composing voice — with it down the
  run has exactly one possible ending, a crash three minutes later. Gemini or
  Codex down still leaves a genuine deliberation between the survivors.

Two fail-safe rules shape the rest of the design:

* **The status document is the truth; the exit code is advisory.** mesh-health
  exits non-zero whenever *any* lane is down, so treating non-zero as "preflight
  failed" would throw away exactly the detail needed to decide.
* **An indeterminate preflight never blocks.** If the script is missing or its
  output is unparseable, the council runs anyway and fails on its own honest
  terms. A health check must not become a new way to lose the daily post.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from vision.config import Settings, VisionEnv, get_settings
from vision.logging_setup import configure_logging, get_logger
from vision.ops.alerts import AlertKind, build_alerter

_log = get_logger(__name__)

# The lanes VISION depends on, and the name each carries in mesh-status.json.
# Listed explicitly (rather than trusting whatever keys the file happens to have)
# so a truncated or older status document reads as "these are missing" instead of
# "everything present is fine".
_REQUIRED_LANES: tuple[str, ...] = ("claude", "gemini", "codex")

# The lane that composes the finished post. Its death is the only one that makes
# the run pointless rather than merely thinner — see the module docstring.
_COMPOSER_LANE = "claude"

# mesh-health probes three CLIs with generous per-lane budgets (90s claude, 90s
# gemini, 180s codex). This bounds the whole thing well clear of their sum so a
# wedged probe cannot eat the council's own systemd timeout.
_HEALTH_TIMEOUT_SECS = 420.0

_STATUS_FILENAME = "mesh-status.json"
_SCRIPT_FILENAME = "godmode-mesh-health.sh"

# Slack allowed when comparing the status file's mtime against the instant the
# probe started. Absorbs coarse filesystem timestamp granularity (FAT/NFS round to
# whole or even 2-second ticks) so a document written moments after the start is
# not misjudged as stale. Far smaller than any real staleness we care about — a
# leftover file is minutes-to-days old, never two seconds.
_MTIME_TOLERANCE_SECS = 2.0


@dataclass(frozen=True)
class LaneHealth:
    """One lane's verdict at one moment. Frozen — a finding is a record, not state."""

    name: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class PreflightResult:
    """The full set of lane verdicts, plus the policy derived from them."""

    lanes: tuple[LaneHealth, ...]

    @property
    def indeterminate(self) -> bool:
        """True when no verdict could be formed at all (no lanes parsed).

        Deliberately distinct from "all healthy": an empty document means *I do
        not know*, and that must never be read as good news.
        """
        return not self.lanes

    @property
    def dead(self) -> tuple[LaneHealth, ...]:
        """Every lane that reported unhealthy, in the order they were parsed."""
        return tuple(lane for lane in self.lanes if not lane.ok)

    @property
    def should_alert(self) -> bool:
        """Whether the owner should hear about this preflight at all."""
        return bool(self.dead)

    @property
    def should_block(self) -> bool:
        """Whether to abandon the run before spending any model time.

        Only the composer lane blocks. An indeterminate result never blocks (see
        the fail-safe rule in the module docstring).
        """
        if self.indeterminate:
            return False
        return any(lane.name == _COMPOSER_LANE for lane in self.dead)

    def summary(self) -> str:
        """A one-line, human-readable digest naming each dead lane and its reason."""
        if self.indeterminate:
            return "lane health could not be determined"
        if not self.dead:
            return "all lanes healthy"
        return "; ".join(
            f"{lane.name} DOWN ({lane.detail or 'no detail'})" for lane in self.dead
        )


def parse_mesh_status(payload: dict) -> PreflightResult:
    """Turn a ``mesh-status.json`` document into a :class:`PreflightResult`.

    A pure function over already-decoded JSON, so the policy is testable without
    touching a filesystem or spawning the health script.

    Fail-closed on shape: a lane whose key is MISSING, or whose ``ok`` is anything
    other than a real boolean ``True``, counts as DEAD. An older or truncated
    status file must not be able to report a lane healthy by omission — the entire
    bug being fixed here was a health check that said "fine" without checking.
    """
    lanes: list[LaneHealth] = []
    for name in _REQUIRED_LANES:
        entry = payload.get(name)
        if not isinstance(entry, dict):
            lanes.append(
                LaneHealth(name=name, ok=False, detail="absent from the status document")
            )
            continue
        raw_ok = entry.get("ok")
        # `is True` on purpose: a truthy string like "true" is malformed input, and
        # guessing in the healthy direction is how a lane stays silently broken.
        ok = raw_ok is True
        detail = str(entry.get("result", "")).strip()
        lanes.append(LaneHealth(name=name, ok=ok, detail=detail))
    return PreflightResult(lanes=tuple(lanes))


def run_mesh_health(
    *,
    script: Path,
    status_file: Path,
    brahmastra_home: Path | None = None,
    timeout: float = _HEALTH_TIMEOUT_SECS,
    bash_executable: str = "bash",
) -> PreflightResult:
    """Run Brahmastra's mesh health script and parse the status file it writes.

    The script's exit code is intentionally IGNORED as a pass/fail signal — it
    encodes *which* lane is down (0 healthy, 1 claude, 2 gemini, 3 codex, 4 two or
    more), and the status document carries the same information with reasons
    attached. We run the script for its side effect (a fresh status file) and then
    read that file.

    Any failure to produce an opinion — script missing, timeout, unreadable or
    malformed JSON — returns an INDETERMINATE result, which never blocks.
    """
    # Stamped BEFORE the run so the freshness check below can tell "the script
    # wrote this just now" from "this is left over from a previous run".
    started_at = time.time()

    # mesh-health resolves its own state dir as "$HOME/.claude/state". With a
    # SHARED Brahmastra core the service user's real HOME is /home/<app>, so the
    # script would write its status into the app's private state while we read
    # the shared one — every run then looks stale and the preflight is
    # permanently indeterminate (observed on the VPS the moment VISION was
    # pointed at /opt/brahmastra). Overriding HOME for THIS SUBPROCESS ONLY —
    # not the unit, not the app — makes the probe write exactly where we read.
    # The rest of the environment is inherited, PATH above all, or the script
    # cannot find the CLIs it exists to probe.
    env = None
    if brahmastra_home is not None:
        env = {**os.environ, "HOME": str(brahmastra_home)}

    try:
        subprocess.run(
            [bash_executable, str(script)],
            env=env,
            # DEVNULL, not capture_output: nothing reads this output (the status
            # file is the interface), and a CLI in a retry storm can emit a lot of
            # diagnostics we would otherwise buffer in memory for nothing.
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _log.warning("brahmastra mesh health timed out; preflight is indeterminate")
        return PreflightResult(lanes=())
    except (OSError, subprocess.SubprocessError) as exc:
        # Launch failure (bash missing, script absent/unreadable). Class only.
        _log.warning(
            "brahmastra mesh health could not run (%s); preflight is indeterminate",
            exc.__class__.__name__,
        )
        return PreflightResult(lanes=())

    # FRESHNESS GATE. The script can run, fail early, and never rewrite the status
    # file — leaving a document from a previous run sitting there. Reading it as
    # current truth is worse than having no opinion in BOTH directions: a stale
    # "claude down" blocks a council whose lane has since recovered, and a stale
    # "all healthy" hides today's outage. The file is evidence only if THIS
    # invocation produced it.
    try:
        written_at = status_file.stat().st_mtime
    except OSError as exc:
        _log.warning(
            "brahmastra mesh status missing after the probe (%s); preflight indeterminate",
            exc.__class__.__name__,
        )
        return PreflightResult(lanes=())

    if written_at < started_at - _MTIME_TOLERANCE_SECS:
        _log.warning(
            "brahmastra mesh status was not refreshed by this run; preflight indeterminate"
        )
        return PreflightResult(lanes=())

    try:
        payload = json.loads(status_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # NOTE: a malformed document here was a real defect — mesh-health wrote raw
        # multi-line probe output into JSON strings, so the file was invalid
        # precisely when a lane had failed. Fixed upstream; this stays fail-safe.
        _log.warning(
            "brahmastra mesh status unreadable (%s); preflight is indeterminate",
            exc.__class__.__name__,
        )
        return PreflightResult(lanes=())

    if not isinstance(payload, dict):
        _log.warning("brahmastra mesh status was not an object; preflight indeterminate")
        return PreflightResult(lanes=())

    return parse_mesh_status(payload)


def preflight(settings: Settings | None = None) -> PreflightResult:
    """Run the lane preflight using paths resolved from configuration.

    Outside ``live`` mode this is INERT and returns an indeterminate result — no
    subprocess, no probe. Every other VISION job holds the same line (the token,
    canary and daily jobs all suppress their side effects in ``dry_run``), and
    here it matters twice over: the health script drives three real model CLIs, so
    a preflight that ran in dry_run would make the test suite call live models and
    burn tokens on every run. Indeterminate never blocks, so a dev checkout
    behaves exactly as it did before the preflight existed.
    """
    settings = settings or get_settings()
    if settings.vision_env is not VisionEnv.LIVE:
        _log.debug("preflight skipped outside live mode; result is indeterminate")
        return PreflightResult(lanes=())

    scripts_dir = Path(settings.brahmastra_scripts_dir).expanduser()
    # mesh-health writes its status next to the other Brahmastra state, which
    # sits at <core>/.claude/state — a sibling of the scripts dir.
    state_dir = scripts_dir.parent / "state"
    # The core is the parent of the .claude dir, i.e. what HOME must look like to
    # the script (~/.claude/... then resolves to the shared install). For a
    # per-user layout this is simply the user's home and the override is a no-op.
    return run_mesh_health(
        script=scripts_dir / _SCRIPT_FILENAME,
        status_file=state_dir / _STATUS_FILENAME,
        brahmastra_home=scripts_dir.parent.parent,
    )


def report_lanes(result: PreflightResult, *, alerter: object | None = None) -> bool:
    """Alert the owner about dead lanes; return whether an alert was dispatched.

    Stays SILENT in two cases, both deliberate:

    * **All healthy** — an all-clear every morning is how an owner learns to
      ignore the channel that will one day carry the real warning.
    * **Indeterminate** — "the health check could not run" is not a lane outage.
      Alerting on it would cry wolf every time the script is missing on a dev box.

    Never raises: reporting bad news must not itself become bad news.
    """
    if not result.should_alert:
        return False

    try:
        sink = alerter if alerter is not None else build_alerter()
        # Subject names only the dead lanes, not their reasons, so it stays stable
        # across runs and the alerter's dedup window can suppress a daily outage.
        # The reasons live in the detail body, where varying text costs nothing.
        subject = f"AI lane preflight: {', '.join(lane.name for lane in result.dead)} down"
        sink.alert(  # type: ignore[attr-defined]
            AlertKind.DAILY_RUN_FAILURE,
            subject,
            (
                f"Brahmastra lane health: {result.summary()}.\n\n"
                "The council composes through the Claude lane, so a Claude outage "
                "blocks the daily post entirely; Gemini or Codex down only thins "
                "the deliberation.\n"
            ),
        )
        return True
    except Exception as exc:  # noqa: BLE001 - reporting must never crash the caller
        _log.error("lane preflight alert failed to deliver: %s", exc.__class__.__name__)
        return False


def main(argv: Sequence[str] | None = None) -> int:
    """``vision-preflight`` entry point — the early-warning job (§17).

    Scheduled ahead of the council so a dead lane is reported while there is still
    time to re-authenticate before the daily content run.

    Returns ``0`` even when lanes are down: nothing about the PREFLIGHT failed, and
    a non-zero exit would additionally trip ``OnFailure=`` and mail the owner a
    second time about the outage this job just reported. The alert is the signal.
    """
    configure_logging()
    result = preflight()

    if result.indeterminate:
        _log.warning("lane preflight indeterminate; council will run unguarded")
        return 0

    _log.info("lane preflight complete", extra={"summary": result.summary()})
    report_lanes(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

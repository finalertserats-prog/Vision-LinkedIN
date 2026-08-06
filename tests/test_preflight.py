"""Lane preflight — ask Brahmastra whether the AI lanes are alive, before running.

WHY this exists (incident 2026-08-06): the council spent two minutes deliberating
against a Claude lane whose OAuth token had expired, then crashed in the composer.
Brahmastra was already installed on the same box with a health checker — it just
was not consulted, and (until the companion fix) would have lied anyway, because
it hardcoded the Claude lane as healthy without ever probing it.

This module closes the loop: run Brahmastra's ``godmode-mesh-health.sh``, read its
``mesh-status.json``, and decide before burning any model time.

The policy under test:
  * ANY dead lane is worth an alert — a two-voice council is a degraded council;
  * only a dead CLAUDE lane BLOCKS, because Claude is the composer and no post is
    possible without it. Gemini or Codex down still leaves a real deliberation.

Everything here is hermetic: the health script is never actually executed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vision.ops.preflight import (
    LaneHealth,
    PreflightResult,
    parse_mesh_status,
)

# A status document in the exact shape godmode-mesh-health.sh writes — captured
# from the VPS on 2026-08-06 with the Claude lane genuinely down. Pinned as a
# fixture so a change to that file's schema breaks here rather than in production.
_VPS_INCIDENT_STATUS = {
    "timestamp": "2026-08-06T18:19:46+00:00",
    "claude": {
        "ok": False,
        "version": "2.1.142 (Claude Code)",
        "result": (
            "FAIL Failed to authenticate. API Error: 401 OAuth access token has "
            "expired. Re-authenticate to continue."
        ),
    },
    "gemini": {"ok": True, "result": "OK"},
    "codex": {"ok": True, "result": "OK"},
}

_ALL_HEALTHY = {
    "timestamp": "2026-08-06T19:00:00+00:00",
    "claude": {"ok": True, "version": "2.1.142", "result": "OK"},
    "gemini": {"ok": True, "result": "OK"},
    "codex": {"ok": True, "result": "OK"},
}


def _status(**lane_ok: bool) -> dict:
    """Build a status document with the given per-lane ok flags."""
    return {
        "timestamp": "2026-08-06T19:00:00+00:00",
        **{name: {"ok": ok, "result": "OK" if ok else "FAIL"} for name, ok in lane_ok.items()},
    }


# --- parsing Brahmastra's status document ------------------------------------


def test_parses_every_lane_from_the_status_document() -> None:
    result = parse_mesh_status(_ALL_HEALTHY)

    assert {lane.name for lane in result.lanes} == {"claude", "gemini", "codex"}


def test_the_vps_incident_document_reports_claude_dead_and_the_others_alive() -> None:
    result = parse_mesh_status(_VPS_INCIDENT_STATUS)

    assert [lane.name for lane in result.dead] == ["claude"]


def test_a_dead_lane_keeps_the_reason_so_the_alert_can_explain_itself() -> None:
    result = parse_mesh_status(_VPS_INCIDENT_STATUS)

    assert "401" in result.dead[0].detail


def test_a_missing_lane_key_counts_as_dead_rather_than_absent() -> None:
    """A truncated/older status file must not read as 'all fine' (fail-closed)."""
    result = parse_mesh_status({"timestamp": "t", "gemini": {"ok": True}})

    assert {lane.name for lane in result.dead} == {"claude", "codex"}


def test_a_non_boolean_ok_value_is_not_treated_as_healthy() -> None:
    """Only a real ``true`` means healthy — a string "true" is malformed input."""
    result = parse_mesh_status(_status(claude="true", gemini=True, codex=True))  # type: ignore[arg-type]

    assert [lane.name for lane in result.dead] == ["claude"]


# --- the block/proceed policy ------------------------------------------------


def test_all_lanes_healthy_neither_blocks_nor_alerts() -> None:
    result = parse_mesh_status(_ALL_HEALTHY)

    assert not result.should_block
    assert not result.should_alert


def test_a_dead_claude_blocks_the_run() -> None:
    """Claude composes the post; without it the run can only end in a crash."""
    result = parse_mesh_status(_VPS_INCIDENT_STATUS)

    assert result.should_block


@pytest.mark.parametrize("dead_lane", ["gemini", "codex"])
def test_a_dead_peer_lane_alerts_but_does_not_block(dead_lane: str) -> None:
    """Two live voices still make a real deliberation — degrade, don't cancel."""
    flags = {"claude": True, "gemini": True, "codex": True}
    flags[dead_lane] = False

    result = parse_mesh_status(_status(**flags))

    assert result.should_alert
    assert not result.should_block


def test_the_summary_names_the_dead_lanes_for_the_alert_body() -> None:
    result = parse_mesh_status(_status(claude=True, gemini=False, codex=False))

    summary = result.summary()

    assert "gemini" in summary and "codex" in summary


# --- running the real health script ------------------------------------------


def test_running_the_health_script_reads_the_status_file_it_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The script's EXIT CODE is advisory; the status document is the truth.

    mesh-health exits non-zero whenever any lane is down, so treating a non-zero
    exit as "preflight failed" would discard the very detail we need.
    """
    import subprocess

    status_file = tmp_path / "mesh-status.json"
    status_file.write_text(json.dumps(_VPS_INCIDENT_STATUS), encoding="utf-8")

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess:
        # Exit 1 == "claude unhealthy" in mesh-health's contract.
        return subprocess.CompletedProcess(args=[], returncode=1, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    from vision.ops.preflight import run_mesh_health

    result = run_mesh_health(script=tmp_path / "mesh.sh", status_file=status_file)

    assert result.should_block


def test_an_unreadable_status_file_does_not_block_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken PREFLIGHT must not become a new way to lose the daily post.

    The preflight is an early-warning optimisation. If it cannot form an opinion
    (script missing, JSON unparseable), the council should still run and fail on
    its own honest terms — never be cancelled by its own health check.
    """
    import subprocess

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args=[], returncode=127, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    from vision.ops.preflight import run_mesh_health

    result = run_mesh_health(
        script=tmp_path / "missing.sh", status_file=tmp_path / "nope.json"
    )

    assert not result.should_block
    assert result.indeterminate


def test_lane_health_is_immutable() -> None:
    """Preflight findings are a record of a moment; nothing should edit them."""
    lane = LaneHealth(name="claude", ok=False, detail="401")

    with pytest.raises(Exception):
        lane.ok = True  # type: ignore[misc]


def test_result_with_no_lanes_is_indeterminate_not_healthy() -> None:
    """An empty document is 'I don't know', which must never read as 'all good'."""
    result = PreflightResult(lanes=())

    assert result.indeterminate
    assert not result.should_block


# --- the standalone `vision-preflight` job (early warning at 02:00) ----------


class FakeAlerter:
    """Captures alerts instead of sending them."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, str, str]] = []

    def alert(self, kind: object, subject: str, detail: str) -> bool:
        self.calls.append((kind, subject, detail))
        return True


def test_preflight_job_alerts_when_a_lane_is_down() -> None:
    from vision.ops.preflight import report_lanes

    alerter = FakeAlerter()
    report_lanes(parse_mesh_status(_VPS_INCIDENT_STATUS), alerter=alerter)

    assert len(alerter.calls) == 1
    assert "claude" in alerter.calls[0][2].lower()


def test_preflight_job_stays_quiet_when_every_lane_is_healthy() -> None:
    """An all-clear is not news; alerting on it would train the owner to ignore."""
    from vision.ops.preflight import report_lanes

    alerter = FakeAlerter()
    report_lanes(parse_mesh_status(_ALL_HEALTHY), alerter=alerter)

    assert alerter.calls == []


def test_preflight_job_stays_quiet_when_it_could_not_form_an_opinion() -> None:
    """Indeterminate is not a lane outage — alerting on it would be crying wolf."""
    from vision.ops.preflight import report_lanes

    alerter = FakeAlerter()
    report_lanes(PreflightResult(lanes=()), alerter=alerter)

    assert alerter.calls == []


def test_preflight_job_subject_is_stable_so_a_daily_outage_dedups() -> None:
    from vision.ops.preflight import report_lanes

    alerter = FakeAlerter()
    result = parse_mesh_status(_VPS_INCIDENT_STATUS)
    report_lanes(result, alerter=alerter)
    report_lanes(result, alerter=alerter)

    assert alerter.calls[0][1] == alerter.calls[1][1]


def test_preflight_job_never_propagates_an_alerting_failure() -> None:
    """Reporting bad news must not itself become bad news."""
    from vision.ops.preflight import report_lanes

    class Exploding:
        def alert(self, *_args: object, **_kwargs: object) -> bool:
            raise RuntimeError("dead channel")

    # Assert: returns normally rather than raising.
    assert report_lanes(parse_mesh_status(_VPS_INCIDENT_STATUS), alerter=Exploding()) is False


# --- the council gate (block before burning model time) ----------------------


def test_council_exits_non_zero_without_running_when_the_composer_lane_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point: don't spend two minutes to crash at the composer.

    A non-zero exit is deliberate — it puts the unit in `failed`, which trips
    OnFailure= and mails the owner the log tail (carrying the 401 reason below).
    That is the single notification for this case; the gate does not also alert.
    """
    import vision.cli.council as council_cli

    ran = False

    def never_run(*_args: object, **_kwargs: object) -> object:
        nonlocal ran
        ran = True
        raise AssertionError("the council must not deliberate with a dead composer")

    monkeypatch.setattr(
        council_cli, "preflight", lambda *_a, **_k: parse_mesh_status(_VPS_INCIDENT_STATUS)
    )
    monkeypatch.setattr(council_cli, "run_council_cli", never_run)

    exit_code = council_cli.main([])

    assert exit_code == 1
    assert ran is False


def test_council_proceeds_when_only_a_peer_lane_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A two-voice deliberation is degraded, not impossible — let it run."""
    import vision.cli.council as council_cli

    monkeypatch.setattr(
        council_cli,
        "preflight",
        lambda *_a, **_k: parse_mesh_status(_status(claude=True, gemini=True, codex=False)),
    )
    seen: list[bool] = []
    monkeypatch.setattr(
        council_cli,
        "run_council_cli",
        lambda *_a, **_k: seen.append(True) or _FakeResult(),
    )

    council_cli.main([])

    assert seen == [True]


def test_council_proceeds_when_the_preflight_cannot_form_an_opinion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken health check must never be the reason a post did not happen."""
    import vision.cli.council as council_cli

    monkeypatch.setattr(council_cli, "preflight", lambda *_a, **_k: PreflightResult(lanes=()))
    seen: list[bool] = []
    monkeypatch.setattr(
        council_cli,
        "run_council_cli",
        lambda *_a, **_k: seen.append(True) or _FakeResult(),
    )

    council_cli.main([])

    assert seen == [True]


class _FakeResult:
    """Minimal stand-in for CouncilRunResult's logged attributes."""

    draft_id = "draft-1"
    content_mode = "council"
    email_sent = True


# --- dry-run must never touch a real CLI -------------------------------------


def test_preflight_is_inert_outside_live_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """dry_run stays side-effect free, exactly like the token/canary/daily jobs.

    Without this the preflight shells out to Brahmastra's mesh-health script on
    every `main()` call — including from the test suite, which then invokes three
    REAL model CLIs. Caught by the suite's runtime tripling (47s -> 132s).
    """
    import subprocess

    import vision.ops.preflight as preflight_mod
    from vision.config import Settings

    def explode(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("dry_run must not spawn the health script")

    monkeypatch.setattr(subprocess, "run", explode)

    result = preflight_mod.preflight(Settings(_env_file=None))  # defaults to dry_run

    assert result.indeterminate
    assert not result.should_block


# --- staleness (Codex review: a leftover status file is not today's truth) ---


def test_a_status_file_the_run_did_not_refresh_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A script that fails without rewriting the file must not yield a verdict.

    Otherwise yesterday's "claude down" blocks a council whose lane has since
    recovered, and yesterday's "all healthy" hides today's outage. The file is
    only evidence if THIS invocation produced it.
    """
    import os
    import subprocess
    import time

    status_file = tmp_path / "mesh-status.json"
    status_file.write_text(json.dumps(_VPS_INCIDENT_STATUS), encoding="utf-8")
    # Age it an hour: a leftover from a previous run.
    stale = time.time() - 3600
    os.utime(status_file, (stale, stale))

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess:
        # Ran, failed early, wrote nothing.
        return subprocess.CompletedProcess(args=[], returncode=2, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    from vision.ops.preflight import run_mesh_health

    result = run_mesh_health(script=tmp_path / "mesh.sh", status_file=status_file)

    assert result.indeterminate
    assert not result.should_block


def test_a_status_file_refreshed_by_this_run_is_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The freshness guard must not reject a genuinely current document."""
    import subprocess

    status_file = tmp_path / "mesh-status.json"

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess:
        # Simulate the script doing its job: write the status during the run.
        status_file.write_text(json.dumps(_VPS_INCIDENT_STATUS), encoding="utf-8")
        return subprocess.CompletedProcess(args=[], returncode=1, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    from vision.ops.preflight import run_mesh_health

    result = run_mesh_health(script=tmp_path / "mesh.sh", status_file=status_file)

    assert not result.indeterminate
    assert result.should_block


# --- shared Brahmastra core: the probe must write where we read -------------


def test_health_script_runs_with_home_pointed_at_the_brahmastra_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """mesh-health writes to $HOME/.claude/state — so HOME decides where.

    Under a SHARED core (/opt/brahmastra/.claude) the service user's real HOME is
    /home/<app>, so the script would write its status to the app's private state
    dir while we read the shared one — and every run would look stale. Scoping
    HOME to the core for THIS subprocess (not the whole service) makes the probe
    write exactly where the status is read from.
    """
    import subprocess

    captured: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
        captured.update(kwargs)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    from vision.ops.preflight import run_mesh_health

    core = tmp_path / "brahmastra"
    scripts = core / ".claude" / "scripts"
    run_mesh_health(
        script=scripts / "mesh.sh",
        status_file=core / ".claude" / "state" / "mesh-status.json",
        brahmastra_home=core,
    )

    env = captured.get("env")
    assert isinstance(env, dict)
    assert env["HOME"] == str(core)
    # The rest of the environment must survive — PATH especially, or the script
    # cannot find the CLIs it is meant to probe.
    assert "PATH" in env


def test_health_script_inherits_the_ambient_home_when_no_core_is_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-user installs keep working untouched — no env override at all."""
    import subprocess

    captured: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
        captured.update(kwargs)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    from vision.ops.preflight import run_mesh_health

    run_mesh_health(script=tmp_path / "mesh.sh", status_file=tmp_path / "s.json")

    assert captured.get("env") is None

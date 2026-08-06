"""``vision-alert-failure`` — the systemd ``OnFailure=`` handler.

WHY this exists (incident 2026-08-06): ``vision-council.service`` crashed at
02:30 UTC. It fail-closed correctly, exited non-zero, and systemd dutifully marked
the unit ``failed`` — and then nothing happened. Nobody was told. The only symptom
the owner ever saw was that the morning approval email did not arrive, and the
cause was only found by reading the log by hand hours later.

The alerting seam (:mod:`vision.ops.alerts`) already existed and was already wired
for the publisher, the token job and the canary. This module is the missing
adapter: systemd hands it the name of the unit that failed, and it turns that into
an ops alert carrying the tail of that unit's own log — so a failed run *reports
itself*, with the traceback already in the body.

Contract under test:
  * the alert names the failed unit and carries its log tail;
  * the subject is STABLE (no timestamp) so the alerter's dedup window can
    actually suppress a flapping unit instead of spamming every tick;
  * the handler NEVER raises and NEVER exits non-zero — it is the last thing
    standing after a failure, so it must not become a second failure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vision.ops.alerts import AlertKind
from vision.ops.job_failure import kind_for_unit, main, read_log_tail


class FakeAlerter:
    """Captures ``alert`` calls instead of sending anything (BRD §18)."""

    def __init__(self, *, explode: bool = False) -> None:
        self.calls: list[tuple[AlertKind, str, str]] = []
        self._explode = explode

    def alert(self, kind: AlertKind, subject: str, detail: str) -> bool:
        if self._explode:
            raise RuntimeError("channel exploded")
        self.calls.append((kind, subject, detail))
        return True


# --- kind_for_unit: routing a unit name to an alert kind ---------------------


def test_publisher_failure_routes_to_the_publish_alert_kind() -> None:
    assert kind_for_unit("vision-publisher.service") is AlertKind.PUBLISH_FAILURE


def test_token_failure_routes_to_the_reauth_alert_kind() -> None:
    assert kind_for_unit("vision-token.service") is AlertKind.TOKEN_REAUTH_NEEDED


def test_an_unrecognised_unit_falls_back_to_the_daily_run_kind() -> None:
    """An unknown unit must still alert — never be dropped for lack of a mapping."""
    assert kind_for_unit("vision-something-new.service") is AlertKind.DAILY_RUN_FAILURE


# --- read_log_tail: pulling the diagnostic into the alert body ---------------


def test_log_tail_returns_the_last_lines_of_the_units_own_log(tmp_path: Path) -> None:
    (tmp_path / "vision-council.log").write_text(
        "\n".join(f"line {n}" for n in range(1, 101)), encoding="utf-8"
    )

    tail = read_log_tail(tmp_path, "vision-council.service", max_lines=3)

    assert tail.splitlines() == ["line 98", "line 99", "line 100"]


def test_log_tail_is_a_plain_note_when_no_log_file_exists(tmp_path: Path) -> None:
    """A missing log must not stop the alert — the unit name alone is still news."""
    tail = read_log_tail(tmp_path, "vision-council.service", max_lines=3)

    assert "vision-council" in tail


# --- main: the entry point systemd actually calls ----------------------------


def test_alert_names_the_failed_unit_and_carries_its_log_tail(tmp_path: Path) -> None:
    (tmp_path / "vision-council.log").write_text(
        "RuntimeError: Council compose produced no usable post", encoding="utf-8"
    )
    alerter = FakeAlerter()

    exit_code = main(
        ["vision-council.service"], alerter=alerter, log_dir=tmp_path
    )

    assert exit_code == 0
    kind, subject, detail = alerter.calls[0]
    assert kind is AlertKind.DAILY_RUN_FAILURE
    assert "vision-council.service" in subject
    assert "Council compose produced no usable post" in detail


def test_subject_is_stable_across_runs_so_dedup_can_suppress_a_flap(
    tmp_path: Path,
) -> None:
    """A timestamped subject would defeat the alerter's dedup key (kind+subject).

    The publisher ticks every five minutes; a subject that changed every run would
    turn one broken publisher into twelve emails an hour.
    """
    alerter = FakeAlerter()

    main(["vision-publisher.service"], alerter=alerter, log_dir=tmp_path)
    main(["vision-publisher.service"], alerter=alerter, log_dir=tmp_path)

    assert alerter.calls[0][1] == alerter.calls[1][1]


def test_missing_unit_argument_still_alerts_rather_than_staying_silent(
    tmp_path: Path,
) -> None:
    """A mis-wired OnFailure= line must not turn into silence — that is the bug."""
    alerter = FakeAlerter()

    exit_code = main([], alerter=alerter, log_dir=tmp_path)

    assert exit_code == 0
    assert len(alerter.calls) == 1


def test_handler_never_propagates_an_alerting_failure(tmp_path: Path) -> None:
    """The failure handler must never become a second failure (§22.9 fail-safe)."""
    exit_code = main(
        ["vision-council.service"], alerter=FakeAlerter(explode=True), log_dir=tmp_path
    )

    assert exit_code == 0


# --- unit-name validation (Codex review: path traversal) ---------------------
# `vision-alert-failure` is a console script, so it is invocable by hand, not only
# by systemd. Its argument becomes a filename whose CONTENTS are then emailed —
# so an unvalidated name is an arbitrary-file-read that exfiltrates over SMTP.


@pytest.mark.parametrize(
    "hostile",
    ["../outside/secret", r"..\outside\secret", "vision-council;rm"],
    ids=["posix-traversal", "windows-traversal", "metacharacters"],
)
def test_a_hostile_unit_name_never_reads_a_file_outside_the_log_dir(
    tmp_path: Path, hostile: str
) -> None:
    """The traversal targets a file that REALLY exists, so a miss would be a leak.

    ``log_dir / "../outside/secret.log"`` resolves to a genuine file here — an
    unvalidated handler would read it and put its contents in an outbound email.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.log").write_text("SENSITIVE", encoding="utf-8")
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    tail = read_log_tail(log_dir, hostile)

    assert "SENSITIVE" not in tail


def test_an_absolute_unit_name_never_escapes_the_log_dir(tmp_path: Path) -> None:
    """An absolute path would make ``log_dir / unit`` discard log_dir entirely."""
    secret = tmp_path / "absolute-secret.log"
    secret.write_text("SENSITIVE", encoding="utf-8")
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    tail = read_log_tail(log_dir, str(tmp_path / "absolute-secret"))

    assert "SENSITIVE" not in tail


def test_a_hostile_unit_name_still_produces_an_alert(tmp_path: Path) -> None:
    """Rejecting the name must not reject the alert — silence is the bug."""
    alerter = FakeAlerter()

    exit_code = main(["../../../etc/passwd"], alerter=alerter, log_dir=tmp_path)

    assert exit_code == 0
    assert len(alerter.calls) == 1

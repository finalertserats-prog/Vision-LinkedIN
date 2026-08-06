"""``vision-alert-failure`` — turn a failed systemd unit into an ops alert (§17).

WHY this module exists (incident 2026-08-06): ``vision-council.service`` crashed
at 02:30 UTC because a shelled-out CLI's OAuth token had expired. Every layer did
its job — the composer fail-closed, the CLI exited non-zero, systemd marked the
unit ``failed`` — and then the chain simply stopped. Nobody was notified. The only
symptom the owner saw was a missing approval email, and the cause took a manual
log read to find.

VISION already had an alerting seam (:mod:`vision.ops.alerts`) wired into the
publisher, the token refresher and the canary. What was missing was the *generic*
adapter: something systemd can point ``OnFailure=`` at for ANY job, which names
the unit that died and attaches the tail of that unit's own log so the alert
arrives with the traceback already in it.

Design notes:

* **Stable subject.** The dedup key is ``kind + subject``, so the subject carries
  the unit name and NOTHING time-varying. A timestamped subject would defeat
  suppression entirely and turn one broken five-minute publisher into a dozen
  emails an hour (NFR-08 "actionable, not noisy"). The timestamp lives in the
  detail body instead, where it costs nothing.
* **Never fails.** This is the last thing standing after something already broke,
  so every path returns ``0`` and no exception escapes. A failure handler that
  can itself fail is just a second silent failure.
* **Log tail, not journal.** The units already redirect stdout/stderr to
  ``<log_dir>/<unit>.log`` (and the JSON logger redacts secrets on the way in), so
  reading that file needs no journal access, no extra group membership, and
  yields exactly the text a human would have gone looking for anyway.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from vision.logging_setup import configure_logging, get_logger
from vision.ops.alerts import AlertKind, build_alerter

_log = get_logger(__name__)

# Where the systemd units append their stdout/stderr. Config-over-code (§22.6) so
# a non-standard deployment can point the handler at its own log directory.
_LOG_DIR_ENV = "VISION_LOG_DIR"
_DEFAULT_LOG_DIR = "/opt/vision/logs"

# How much of the failed unit's log to attach. Enough to carry a full traceback
# plus the lines that led into it; bounded so a runaway log can't produce an
# unsendable alert body.
_TAIL_LINES = 40
# Hard ceiling on bytes read off the end of the log, so a single pathological
# line (a dumped payload) cannot blow up the alert either.
_TAIL_BYTES = 16_000

# Which alert kind a given unit's failure represents. An unmapped unit falls back
# to DAILY_RUN_FAILURE — being alerted under a slightly generic kind beats not
# being alerted at all, which is the exact bug this module closes.
_KIND_BY_UNIT: dict[str, AlertKind] = {
    "vision-publisher": AlertKind.PUBLISH_FAILURE,
    "vision-token": AlertKind.TOKEN_REAUTH_NEEDED,
}

# Used when systemd invokes the handler without an instance name (a mis-wired
# OnFailure= line). We still alert — a nameless alert beats silence.
_UNKNOWN_UNIT = "unknown-unit"

# The unit name arrives as a command-line argument and is used to BUILD A FILENAME
# whose contents are then emailed out. That makes an unvalidated name an
# arbitrary-file-read with an SMTP exfiltration path — systemd only ever passes a
# real unit name, but this ships as a console script anyone with a shell can run.
# systemd's own unit-name grammar is this alphabet, so nothing legitimate is lost
# by refusing everything else (notably '/', '\' and '..').
_SAFE_UNIT_NAME = re.compile(r"^[A-Za-z0-9_.@-]+$")


def _unit_stem(unit: str) -> str:
    """Strip the ``.service`` suffix from a unit name (``%i`` keeps it)."""
    return unit[: -len(".service")] if unit.endswith(".service") else unit


def is_safe_unit_name(unit: str) -> bool:
    """Return whether ``unit`` is a plausible systemd unit name.

    Rejects path separators, ``..`` segments and anything else outside systemd's
    unit-name alphabet, so a hostile argument can never steer :func:`read_log_tail`
    at a file outside the log directory.
    """
    return bool(_SAFE_UNIT_NAME.fullmatch(unit)) and ".." not in unit


def kind_for_unit(unit: str) -> AlertKind:
    """Map a failed unit name to the :class:`AlertKind` it should alert under.

    Falls back to :attr:`AlertKind.DAILY_RUN_FAILURE` for any unit not in the
    table, so adding a new timer can never silently opt out of alerting.
    """
    return _KIND_BY_UNIT.get(_unit_stem(unit), AlertKind.DAILY_RUN_FAILURE)


def read_log_tail(log_dir: Path, unit: str, max_lines: int = _TAIL_LINES) -> str:
    """Return the last ``max_lines`` of ``<log_dir>/<unit>.log`` as text.

    Returns an explanatory note (never raises, never returns empty) when the log
    is missing or unreadable: the alert must go out regardless — knowing that a
    unit failed is already the news, and the missing log is itself a useful clue.

    Only the last :data:`_TAIL_BYTES` are read, so the handler stays cheap and
    bounded even against a log that has grown to hundreds of megabytes.
    """
    if not is_safe_unit_name(unit):
        # Refuse to touch the filesystem at all for a name we don't trust. The
        # ALERT still goes out (the caller does not depend on this returning a
        # tail) — being told about a bogus invocation is itself useful.
        return "(no log read: the supplied unit name is not a valid systemd unit name)"

    log_path = log_dir / f"{_unit_stem(unit)}.log"
    # Defence in depth behind the name check: even if the alphabet above is ever
    # loosened, a resolved path that escapes log_dir is refused outright.
    try:
        log_path.resolve().relative_to(log_dir.resolve())
    except (OSError, ValueError):
        return "(no log read: resolved log path escapes the configured log directory)"

    try:
        size = log_path.stat().st_size
        with log_path.open("rb") as handle:
            if size > _TAIL_BYTES:
                handle.seek(size - _TAIL_BYTES)
            blob = handle.read()
    except OSError as exc:
        # Missing file, permission denied, unreadable mount — all the same to the
        # caller: no tail available. The exception CLASS and the path we tried are
        # both reported (the path is the diagnosis, and it is an operator-visible
        # deployment path, not a secret); the file's CONTENTS never are, since by
        # definition we could not read them.
        return (
            f"(no log available for {_unit_stem(unit)}: "
            f"{exc.__class__.__name__} reading {log_path})"
        )

    text = blob.decode("utf-8", "ignore")
    lines = text.splitlines()
    if not lines:
        return f"(log for {_unit_stem(unit)} is empty)"
    return "\n".join(lines[-max_lines:])


def _build_detail(unit: str, log_dir: Path) -> str:
    """Compose the alert body: when it failed, which unit, and its log tail.

    The timestamp lives HERE rather than in the subject on purpose — see the
    stable-subject note in the module docstring.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tail = read_log_tail(log_dir, unit)
    return (
        f"Unit {unit} entered a failed state at {now}.\n\n"
        f"Last lines of {_unit_stem(unit)}.log:\n\n{tail}\n"
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    alerter: object | None = None,
    log_dir: Path | None = None,
) -> int:
    """``vision-alert-failure`` entry point; always exits ``0``.

    systemd invokes this as ``ExecStart=... %i`` from a templated ``OnFailure=``
    unit, so ``argv[0]`` is the name of the unit that failed.

    Args:
        argv: Command-line args (the failed unit name). Defaults to the real
            process args; an empty list is tolerated and still alerts.
        alerter: Injected alert sink (a fake in tests). Defaults to the
            configured :func:`~vision.ops.alerts.build_alerter`.
        log_dir: Where unit logs live. Defaults to ``$VISION_LOG_DIR``.

    Returns:
        Always ``0``. A non-zero exit here would put the *handler* into a failed
        state too, which tells the owner nothing new and risks an alert loop.
    """
    # Logging setup is itself I/O (it opens handlers and reads config), so it can
    # raise on a malformed env or an unwritable target. Outside a guard that would
    # abort the handler BEFORE the alert — the one outcome this module exists to
    # prevent. Alerting matters more than logging about alerting, so a broken
    # logger degrades to no logs, never to no alert.
    try:
        configure_logging()
    except Exception:  # noqa: BLE001 - logging must never block the alert
        pass

    args = list(sys.argv[1:] if argv is None else argv)
    unit = args[0].strip() if args and args[0].strip() else _UNKNOWN_UNIT
    resolved_log_dir = log_dir or Path(
        os.environ.get(_LOG_DIR_ENV, _DEFAULT_LOG_DIR)
    )

    try:
        _log.error("systemd unit failed; sending ops alert", extra={"unit": unit})
        sink = alerter if alerter is not None else build_alerter()
        # Subject: stable, unit-scoped, no timestamp — the dedup window depends on it.
        sink.alert(  # type: ignore[attr-defined]
            kind_for_unit(unit),
            f"{unit} failed",
            _build_detail(unit, resolved_log_dir),
        )
    except Exception as exc:  # noqa: BLE001 - a failure handler must never fail
        # Broad by design: this runs only when something has ALREADY broken. Any
        # escape here (bad config, dead mailer, unreadable DB) would replace a
        # reported failure with an unreported one. Log the class and exit clean.
        _log.error(
            "failure alert could not be delivered: %s", exc.__class__.__name__
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

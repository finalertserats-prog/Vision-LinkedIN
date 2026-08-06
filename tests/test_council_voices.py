"""The voice transport must reject CLI diagnostics that masquerade as answers.

WHY this file exists: on 2026-08-06 the Claude CLI's OAuth token expired on the
VPS. The CLI printed ``Failed to authenticate. API Error: 401 OAuth access token
has expired.`` on **stdout** and exited, and :meth:`Voices.ask` — which gates on
"non-empty stdout" — handed that sentence upstream as if it were a model answer.
It became the day's *topic*, then blew up the composer three attempts later. The
run fail-closed correctly, but the logs blamed a "parse miss" and never named the
real cause: a dead lane.

The guard under test closes that gap at the single transport seam: output that is
BOTH short and carries a known CLI-diagnostic signature is treated as a dead lane
(``""``, fail-soft) rather than content. The length ceiling is what keeps a
genuine 2000-character post *about* token expiry from being mistaken for one.

Every test here mocks ``subprocess.run`` — no real CLI is ever invoked (§18).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from vision.config import Settings
from vision.council.voices import CLAUDE, GEMINI, Voices, detect_cli_error

# The verbatim stdout the Claude CLI produced during the 2026-08-06 incident.
# Pinned exactly so a future CLI wording change that slips past the guard shows up
# here as a failing test rather than as another silent bad-content day.
_INCIDENT_OUTPUT = (
    "Failed to authenticate. API Error: 401 OAuth access token has expired. "
    "Re-authenticate to continue."
)


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    """Hermetic Settings with the council dir pinned under the test's tmp dir."""
    base: dict[str, object] = {"BRAHMASTRA_COUNCIL_DIR": str(tmp_path)}
    base.update(overrides)
    # _env_file=None: depend only on code defaults + explicit overrides, never on
    # the developer's real .env.
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


def _stub_stdout(monkeypatch: pytest.MonkeyPatch, stdout: str) -> None:
    """Make every ``subprocess.run`` in the voices module return ``stdout``."""

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout=stdout.encode("utf-8"), stderr=b""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)


# --- detect_cli_error: the pure predicate ------------------------------------


def test_detects_the_expired_oauth_diagnostic_from_the_incident() -> None:
    assert detect_cli_error(_INCIDENT_OUTPUT) is not None


@pytest.mark.parametrize(
    "output",
    [
        "Failed to authenticate. API Error: 401 OAuth access token has been revoked.",
        "Invalid API key. Please run /login.",
        "Not inside a trusted directory and --skip-git-repo-check was not specified.",
        "bash: line 1: codex: command not found",
        "Rate limited. Please try again later.",
    ],
    ids=["revoked", "invalid-key", "untrusted-dir", "missing-binary", "rate-limited"],
)
def test_detects_other_known_cli_diagnostics(output: str) -> None:
    assert detect_cli_error(output) is not None


def test_ignores_a_long_answer_that_merely_discusses_token_expiry() -> None:
    """A real post ABOUT expired tokens must not be mistaken for a dead lane.

    This is the false-positive guard the length ceiling exists for: the signature
    is present, but so are two thousand characters of genuine prose.
    """
    essay = (
        "Every OAuth access token has expired at the worst possible moment for "
        "someone, and the interesting question is who finds out first. "
    ) * 20

    assert detect_cli_error(essay) is None


def test_ignores_a_short_answer_with_no_diagnostic_signature() -> None:
    assert detect_cli_error("Short, but a genuine answer.") is None


@pytest.mark.parametrize(
    "prose",
    [
        "Every API a bank ships is rate limited somewhere, usually at the wrong layer.",
        "The command not found problem is really an onboarding problem in disguise.",
    ],
    ids=["rate-limited-in-prose", "command-not-found-in-prose"],
)
def test_ignores_ordinary_english_that_merely_echoes_a_signature(prose: str) -> None:
    """Signatures use the PUNCTUATED shapes tools emit, not bare English phrases.

    A short, genuine answer that happens to use "rate limited" or "command not
    found" as ordinary words must survive — the guard keys on ": command not
    found" and "rate limited." because that is what the tools actually print.
    """
    assert detect_cli_error(prose) is None


def test_ignores_empty_output() -> None:
    """Empty output is already handled as its own case upstream, not a diagnostic."""
    assert detect_cli_error("   ") is None


# --- Voices.ask: the transport seam ------------------------------------------


def test_ask_returns_empty_when_the_cli_prints_an_auth_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The incident output must reach the caller as a dead lane, not as content."""
    _stub_stdout(monkeypatch, _INCIDENT_OUTPUT)

    answer = Voices(_settings(tmp_path)).ask(CLAUDE, "What should we write about?")

    assert answer == ""


def test_ask_logs_the_dead_lane_at_error_with_the_matched_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A dead lane must be diagnosable from the log alone — that was the real miss."""
    _stub_stdout(monkeypatch, _INCIDENT_OUTPUT)
    caplog.set_level("ERROR", logger="vision.council.voices")

    Voices(_settings(tmp_path)).ask(GEMINI, "prompt")

    assert any(record.levelname == "ERROR" for record in caplog.records)


def test_ask_still_returns_a_genuine_answer_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard must not disturb the happy path."""
    _stub_stdout(monkeypatch, "A perfectly ordinary answer from the model.")

    answer = Voices(_settings(tmp_path)).ask(CLAUDE, "prompt")

    assert answer == "A perfectly ordinary answer from the model."

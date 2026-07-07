"""Unit tests for the Brahmastra CLI adapter (``BrahmastraClient``).

WHY these tests: BRD §18/§22 make tests part of "done" and require external
deps (here the council subprocess) to be *mocked* so NO real model is ever
called. We assert the four contract-critical behaviours from the task:

  1. Robust JSON parsing — plain, code-fenced, and prose-surrounded output.
  2. Non-JSON output fails loudly with ``BrahmastraError`` (§22.5/§22.9).
  3. The correct script + mode is chosen per lane (gemini/codex/claude).
  4. Timeout/empty on the first attempt triggers exactly one retry.

Every test follows AAA (Arrange → Act → Assert) with a single behavioural
assertion focus, and patches ``subprocess.run`` so the tests are hermetic.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from vision.brahmastra.client import BrahmastraClient
from vision.brahmastra.errors import BrahmastraError
from vision.config import Settings

# --- Test fixtures / helpers -----------------------------------------------

# A fixed council directory keeps the expected script paths deterministic and
# independent of the developer's real ~/.claude/council location.
_COUNCIL_DIR = Path("/fake/council")


def _make_settings() -> Settings:
    """Build a Settings object with pinned lanes + council dir for assertions.

    WHY explicit construction (not ``get_settings``): tests must control the
    per-pass lane defaults and the council directory without depending on the
    developer's environment or a real ``.env``.
    """
    return Settings(
        BRAHMASTRA_COUNCIL_DIR=_COUNCIL_DIR,
        MODEL_GENERATE="gemini",
        MODEL_CRITIQUE="codex",
        MODEL_VERIFY="claude",
    )


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    """Return a fake ``CompletedProcess`` mimicking a council-script call."""
    return subprocess.CompletedProcess(
        args=["bash"], returncode=returncode, stdout=stdout, stderr=""
    )


def _client() -> BrahmastraClient:
    """Construct the adapter under test with pinned settings."""
    return BrahmastraClient(_make_settings())


# --- 1. Robust JSON parsing -------------------------------------------------


def test_generate_parses_plain_json_object() -> None:
    # Arrange: the CLI returns a clean JSON object on stdout.
    client = _client()
    with patch("vision.brahmastra.client.subprocess.run") as mock_run:
        mock_run.return_value = _completed('{"hook": "Insight", "confidence": 0.9}')

        # Act.
        result = client.generate("draft the post")

    # Assert: parsed into an equal dict.
    assert result == {"hook": "Insight", "confidence": 0.9}


def test_generate_strips_markdown_code_fences() -> None:
    # Arrange: JSON wrapped in a ```json fenced block (common model formatting).
    fenced = '```json\n{"body": "text", "hashtags": ["#AI"]}\n```'
    client = _client()
    with patch("vision.brahmastra.client.subprocess.run") as mock_run:
        mock_run.return_value = _completed(fenced)

        # Act.
        result = client.generate("draft the post")

    # Assert: fences stripped, object recovered.
    assert result == {"body": "text", "hashtags": ["#AI"]}


def test_generate_extracts_json_amid_surrounding_prose() -> None:
    # Arrange: model wraps the object in chatty prose before and after.
    noisy = 'Sure! Here is the draft:\n{"takeaway": "so what"}\nHope that helps.'
    client = _client()
    with patch("vision.brahmastra.client.subprocess.run") as mock_run:
        mock_run.return_value = _completed(noisy)

        # Act.
        result = client.verify("check claims")

    # Assert: only the balanced object is parsed.
    assert result == {"takeaway": "so what"}


def test_extracts_first_balanced_object_ignoring_nested_and_strings() -> None:
    # Arrange: nested braces + a brace inside a string literal must not confuse
    # the balanced-brace scanner.
    tricky = 'prefix {"outer": {"inner": 1}, "note": "a } brace in a string"} suffix'
    client = _client()
    with patch("vision.brahmastra.client.subprocess.run") as mock_run:
        mock_run.return_value = _completed(tricky)

        # Act.
        result = client.generate("draft")

    # Assert: the full top-level object is returned with nesting intact.
    assert result == {"outer": {"inner": 1}, "note": "a } brace in a string"}


# --- 2. Non-JSON fails loudly ----------------------------------------------


def test_non_json_output_raises_brahmastra_error() -> None:
    # Arrange: the model returns prose with no JSON object at all.
    client = _client()
    with patch("vision.brahmastra.client.subprocess.run") as mock_run:
        mock_run.return_value = _completed("I could not complete that request.")

        # Act / Assert: a contract breach must fail loudly.
        with pytest.raises(BrahmastraError):
            client.generate("draft the post")


def test_malformed_json_object_raises_brahmastra_error() -> None:
    # Arrange: a brace-delimited but syntactically invalid object.
    client = _client()
    with patch("vision.brahmastra.client.subprocess.run") as mock_run:
        mock_run.return_value = _completed('{"hook": "x", bad}')

        # Act / Assert.
        with pytest.raises(BrahmastraError):
            client.generate("draft the post")


def test_json_array_not_object_raises_brahmastra_error() -> None:
    # Arrange: valid JSON but a list, not the object our contract requires.
    # (The extractor finds the object inside array elements, so use a payload
    # with no object at all to exercise the "no JSON object" path.)
    client = _client()
    with patch("vision.brahmastra.client.subprocess.run") as mock_run:
        mock_run.return_value = _completed("[1, 2, 3]")

        # Act / Assert.
        with pytest.raises(BrahmastraError):
            client.generate("draft the post")


def test_json_array_of_object_raises_brahmastra_error() -> None:
    # Arrange: a JSON ARRAY whose first element is an object. The old brace-scan
    # located the first '{' and returned the inner object, silently accepting an
    # array as if it were the contracted object. An array is drift and must be
    # rejected, not unwrapped (§22.5 fail-loudly, JSON object only).
    client = _client()
    with patch("vision.brahmastra.client.subprocess.run") as mock_run:
        mock_run.return_value = _completed('[{"ok": true}]')

        # Act / Assert: the array must fail loudly, not yield {"ok": True}.
        with pytest.raises(BrahmastraError):
            client.generate("draft the post")


def test_unknown_lane_raises_brahmastra_error() -> None:
    # Arrange: an unrecognised lane must fail closed, not mis-route.
    client = _client()

    # Act / Assert: no subprocess should even be attempted.
    with pytest.raises(BrahmastraError):
        client.generate("draft", lane="mistral")


# --- 3. Correct script + mode per lane -------------------------------------


def test_generate_defaults_to_gemini_script_default_mode() -> None:
    # Arrange.
    client = _client()
    with patch("vision.brahmastra.client.subprocess.run") as mock_run:
        mock_run.return_value = _completed('{"ok": true}')

        # Act.
        client.generate("draft the post")

    # Assert: bash invoked with gemini_call.sh in "default" mode.
    cmd = mock_run.call_args.args[0]
    assert cmd[0] == "bash"
    assert cmd[1].endswith("gemini_call.sh")
    assert cmd[2] == "draft the post"
    assert cmd[3] == "default"


def test_critique_defaults_to_codex_script() -> None:
    # Arrange.
    client = _client()
    with patch("vision.brahmastra.client.subprocess.run") as mock_run:
        mock_run.return_value = _completed('{"ok": true}')

        # Act.
        client.critique("tighten the draft")

    # Assert: codex lane → codex_call.sh, "default" mode.
    cmd = mock_run.call_args.args[0]
    assert cmd[1].endswith("codex_call.sh")
    assert cmd[3] == "default"


def test_verify_defaults_to_claude_local_script_with_claude_arg() -> None:
    # Arrange: the claude lane rides local_call.sh with subbing_for="claude".
    client = _client()
    with patch("vision.brahmastra.client.subprocess.run") as mock_run:
        mock_run.return_value = _completed('{"grounded": []}')

        # Act.
        client.verify("verify the claims")

    # Assert.
    cmd = mock_run.call_args.args[0]
    assert cmd[1].endswith("local_call.sh")
    assert cmd[3] == "claude"


def test_explicit_lane_override_selects_that_script() -> None:
    # Arrange: override the generate pass onto the codex lane.
    client = _client()
    with patch("vision.brahmastra.client.subprocess.run") as mock_run:
        mock_run.return_value = _completed('{"ok": true}')

        # Act.
        client.generate("draft", lane="codex")

    # Assert: the override wins over the model_generate default.
    cmd = mock_run.call_args.args[0]
    assert cmd[1].endswith("codex_call.sh")


def test_script_path_uses_configured_council_dir() -> None:
    # Arrange.
    client = _client()
    with patch("vision.brahmastra.client.subprocess.run") as mock_run:
        mock_run.return_value = _completed('{"ok": true}')

        # Act.
        client.generate("draft")

    # Assert: the script path is rooted at BRAHMASTRA_COUNCIL_DIR. The client passes
    # forward-slash (posix) paths to bash, so compare against the posix form (str()
    # would use backslashes on Windows and never match).
    cmd = mock_run.call_args.args[0]
    assert _COUNCIL_DIR.as_posix() in cmd[1]


def test_claude_callable_is_used_instead_of_subprocess_when_injected() -> None:
    # Arrange: inject an in-process Claude callable; subprocess must NOT be hit.
    calls: list[str] = []

    def fake_claude(prompt: str) -> str:
        calls.append(prompt)
        return '{"revised_post": "clean"}'

    client = BrahmastraClient(_make_settings(), claude_callable=fake_claude)
    with patch("vision.brahmastra.client.subprocess.run") as mock_run:
        # Act.
        result = client.verify("verify the claims")

    # Assert: callable served the request, subprocess was never called.
    assert result == {"revised_post": "clean"}
    assert calls == ["verify the claims"]
    mock_run.assert_not_called()


# --- 4. Timeout + one retry -------------------------------------------------


def test_retries_once_after_timeout_then_succeeds() -> None:
    # Arrange: first call times out, second returns valid JSON.
    client = _client()
    with patch("vision.brahmastra.client.subprocess.run") as mock_run:
        mock_run.side_effect = [
            subprocess.TimeoutExpired(cmd="bash", timeout=180.0),
            _completed('{"ok": true}'),
        ]

        # Act.
        result = client.generate("draft the post")

    # Assert: recovered on the retry; exactly two attempts made.
    assert result == {"ok": True}
    assert mock_run.call_count == 2


def test_retries_once_after_empty_output_then_succeeds() -> None:
    # Arrange: first call returns empty stdout (transient), second returns JSON.
    client = _client()
    with patch("vision.brahmastra.client.subprocess.run") as mock_run:
        mock_run.side_effect = [_completed("   "), _completed('{"ok": true}')]

        # Act.
        result = client.critique("tighten")

    # Assert.
    assert result == {"ok": True}
    assert mock_run.call_count == 2


def test_raises_after_both_attempts_time_out() -> None:
    # Arrange: both attempts time out — no usable output at all.
    client = _client()
    with patch("vision.brahmastra.client.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="bash", timeout=180.0)

        # Act / Assert: fails loudly after the single retry is exhausted.
        with pytest.raises(BrahmastraError):
            client.generate("draft the post")

    # Assert: it tried exactly twice (initial + one retry), no infinite loop.
    assert mock_run.call_count == 2


def test_does_not_retry_more_than_once_on_persistent_empty() -> None:
    # Arrange: every attempt returns empty → must stop at two, then raise.
    client = _client()
    with patch("vision.brahmastra.client.subprocess.run") as mock_run:
        mock_run.return_value = _completed("")

        # Act / Assert.
        with pytest.raises(BrahmastraError):
            client.generate("draft")

    assert mock_run.call_count == 2

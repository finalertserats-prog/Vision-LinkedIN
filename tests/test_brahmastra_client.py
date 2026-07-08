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

import io
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from vision.brahmastra.client import BrahmastraClient
from vision.brahmastra.errors import BrahmastraError, ImageGenerationError
from vision.brahmastra.image_client import BrahmastraImageClient
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


# ---------------------------------------------------------------------------
# BrahmastraImageClient — the CONFIRMED-WORKING agy (Antigravity/Gemini) path.
#
# WHY these tests (BRD §18/§22 — tests are part of "done"): agy is THE AI-image
# path (the legacy 'gemini' CLI is DEAD / IneligibleTierError). But a unit test
# must NEVER really launch agy or touch the network — so ``subprocess.run`` is
# always MOCKED. The mock simulates agy's real behaviour: it WRITES a PNG to the
# output path embedded in the command (or, for the failure case, writes nothing).
# We assert:
#   1. Success — a valid PNG appears at the temp path → ``illustrate`` returns it.
#   2. The prompt is text-free style-guided ('no text' / 'no logos' present).
#   3. The invocation shape is the confirmed agy agent form.
#   4. agy saved a JPEG → it is converted to PNG before returning.
#   5. No file produced → ``ImageGenerationError`` (caller degrades to text-only).
#   6. Timeout → one retry, then ``ImageGenerationError``.
# ---------------------------------------------------------------------------

_AGY_BIN = "/fake/agy/bin/agy"

# Style guide pinned so the "text-free" assertion is deterministic and does not
# depend on the developer's environment / real ``.env``.
_STYLE_GUIDE = "minimal, professional, muted palette"


def _image_settings() -> Settings:
    """Build Settings with a pinned agy binary + style guide for assertions."""
    return Settings(AGY_BIN=_AGY_BIN, IMAGE_STYLE_GUIDE=_STYLE_GUIDE)


def _image_client() -> BrahmastraImageClient:
    """Construct the image adapter under test with pinned settings."""
    return BrahmastraImageClient(_image_settings())


def _encode_image(image_format: str) -> bytes:
    """Return encoded bytes for a small solid image in ``image_format``."""
    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), (11, 31, 58)).save(buffer, format=image_format)
    return buffer.getvalue()


def _output_path_from_cmd(cmd: list[str]) -> Path:
    """Extract the absolute output path agy is told to save the PNG to.

    WHY parse it out of the command: the client creates its own tempfile, so the
    mock cannot know the path in advance — it must read it back from the ``-p``
    prompt argument (which embeds 'Save the generated PNG to <ABS_OUTPUT_PATH>')
    exactly as a real agy invocation would.
    """
    prompt = cmd[cmd.index("-p") + 1]
    marker = "Save the generated PNG to "
    start = prompt.index(marker) + len(marker)
    # The path ends right before the '. Confirm the file path' sentence.
    tail = prompt[start:]
    path_str = tail.split(". Confirm", 1)[0].strip()
    return Path(path_str)


def _agy_writes(image_format: str = "PNG"):
    """Return a ``subprocess.run`` side effect that simulates agy saving a file.

    It reads the output path from the command args and writes a real (encoded)
    image there — emulating agy the agent — then returns a success process.
    """
    payload = _encode_image(image_format)

    def _side_effect(cmd, *args, **kwargs):
        out_path = _output_path_from_cmd(cmd)
        out_path.write_bytes(payload)
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="Saved to " + str(out_path), stderr=""
        )

    return _side_effect


def _agy_writes_nothing(cmd, *args, **kwargs):
    """A ``subprocess.run`` side effect where agy exits but saves NO file."""
    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="done", stderr="")


def test_illustrate_returns_png_bytes_when_agy_saves_a_file() -> None:
    # Arrange: agy (mocked) writes a valid PNG to the temp path it is given.
    client = _image_client()
    with patch("vision.brahmastra.image_client.subprocess.run") as mock_run:
        mock_run.side_effect = _agy_writes("PNG")

        # Act.
        data = client.illustrate("an abstract muted horizon")

    # Assert: real PNG bytes come back, and agy was invoked exactly once.
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert mock_run.call_count == 1


def test_illustrate_prompt_is_text_free_style_guided() -> None:
    # Arrange.
    client = _image_client()
    with patch("vision.brahmastra.image_client.subprocess.run") as mock_run:
        mock_run.side_effect = _agy_writes("PNG")

        # Act.
        client.illustrate("an abstract muted horizon")

    # Assert: the -p prompt carries the style guide AND the mandatory text-free
    # negatives so agy/diffusion can never bake in words/logos (§13.6/D10).
    cmd = mock_run.call_args.args[0]
    prompt = cmd[cmd.index("-p") + 1]
    assert _STYLE_GUIDE in prompt
    assert "no text" in prompt
    assert "no words" in prompt
    assert "no letters" in prompt
    assert "no logos" in prompt


def test_illustrate_uses_confirmed_agy_agent_invocation() -> None:
    # Arrange.
    client = _image_client()
    with patch("vision.brahmastra.image_client.subprocess.run") as mock_run:
        mock_run.side_effect = _agy_writes("PNG")

        # Act.
        client.illustrate("an abstract muted horizon")

    # Assert: the exact confirmed-working agent form — the configured binary,
    # --add-dir <abs cwd>, --dangerously-skip-permissions, and a -p agent prompt.
    cmd = mock_run.call_args.args[0]
    assert cmd[0] == _AGY_BIN
    assert "--add-dir" in cmd
    add_dir_value = cmd[cmd.index("--add-dir") + 1]
    assert Path(add_dir_value).is_absolute()
    assert "--dangerously-skip-permissions" in cmd
    assert "-p" in cmd
    prompt = cmd[cmd.index("-p") + 1]
    assert "Use your image generation capability" in prompt
    assert "Save the generated PNG to" in prompt


def test_illustrate_converts_jpeg_output_to_png() -> None:
    # Arrange: agy saves a JPEG (some runs do). It must be converted to PNG so the
    # downstream LinkedIn contract (PNG/JPEG, validated later) is honoured and the
    # returned bytes are a real image, not a text error.
    client = _image_client()
    with patch("vision.brahmastra.image_client.subprocess.run") as mock_run:
        mock_run.side_effect = _agy_writes("JPEG")

        # Act.
        data = client.illustrate("an abstract muted horizon")

    # Assert: the bytes were normalised to PNG.
    assert data.startswith(b"\x89PNG\r\n\x1a\n")


def test_illustrate_raises_when_agy_produces_no_file() -> None:
    # Arrange: agy exits cleanly but saves nothing → no image to return.
    client = _image_client()
    with patch("vision.brahmastra.image_client.subprocess.run") as mock_run:
        mock_run.side_effect = _agy_writes_nothing

        # Act / Assert: a missing file is an image failure the caller degrades on
        # (BRD §13.6 — image never blocks publishing), surfaced as ImageGenerationError.
        with pytest.raises(ImageGenerationError):
            client.illustrate("an abstract muted horizon")


def test_illustrate_raises_when_file_is_not_a_real_image() -> None:
    # Arrange: agy writes a text error message to the path instead of an image.
    client = _image_client()

    def _writes_text(cmd, *args, **kwargs):
        _output_path_from_cmd(cmd).write_bytes(b"model returned an error, not an image")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    with patch("vision.brahmastra.image_client.subprocess.run") as mock_run:
        mock_run.side_effect = _writes_text

        # Act / Assert.
        with pytest.raises(ImageGenerationError):
            client.illustrate("an abstract muted horizon")


def test_illustrate_retries_once_after_timeout_then_succeeds() -> None:
    # Arrange: first agy run times out, the retry saves a valid PNG. A stateful
    # side effect models the transient failure → recovery.
    client = _image_client()
    calls = {"n": 0}
    writer = _agy_writes("PNG")

    def _flaky(cmd, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise subprocess.TimeoutExpired(cmd="agy", timeout=200.0)
        return writer(cmd, *args, **kwargs)

    with patch("vision.brahmastra.image_client.subprocess.run") as mock_run:
        mock_run.side_effect = _flaky

        # Act.
        data = client.illustrate("an abstract muted horizon")

    # Assert: recovered on the retry; exactly two attempts made.
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert mock_run.call_count == 2


def test_illustrate_raises_after_both_attempts_time_out() -> None:
    # Arrange: both attempts time out — agy never produces an image.
    client = _image_client()
    with patch("vision.brahmastra.image_client.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="agy", timeout=200.0)

        # Act / Assert: degrades via ImageGenerationError after the single retry.
        with pytest.raises(ImageGenerationError):
            client.illustrate("an abstract muted horizon")

    # Assert: exactly two attempts (initial + one retry), no infinite loop.
    assert mock_run.call_count == 2


def test_illustrate_does_not_call_agy_when_binary_launch_fails() -> None:
    # Arrange: launching agy raises OSError (binary missing / not executable). This
    # must be normalised to ImageGenerationError, not propagate a raw OSError.
    client = _image_client()
    with patch("vision.brahmastra.image_client.subprocess.run") as mock_run:
        mock_run.side_effect = OSError("agy binary not found")

        # Act / Assert.
        with pytest.raises(ImageGenerationError):
            client.illustrate("an abstract muted horizon")

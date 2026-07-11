"""Unit tests for the hashtag fallback (``vision.council.hashtags``).

WHY these tests: the composing voice often drops the hashtags the compose prompt
asks for, so a decoupled step generates them FROM the finished post. These tests
MOCK the voice transport - NO real model runs. We assert the contract:

  1. a valid reply -> clean, de-duped, capped hashtags;
  2. generic filler and vendor-name tags are dropped;
  3. an empty post or a voice error -> [] (post ships without hashtags, unblocked);
  4. more than the cap is truncated.

Every test is AAA with a single behavioural focus.
"""

from __future__ import annotations

from vision.council.hashtags import HashtagWriter, _parse_hashtags


class _FakeVoices:
    """A Voices stand-in returning a canned reply (or raising) for ask()."""

    def __init__(self, reply: str | Exception) -> None:
        self._reply = reply
        self.calls = 0

    def ask(self, voice: str, prompt: str) -> str:
        self.calls += 1
        if isinstance(self._reply, Exception):
            raise self._reply
        return self._reply


def _writer(reply: str | Exception) -> HashtagWriter:
    return HashtagWriter(voices=_FakeVoices(reply))  # type: ignore[arg-type]


# --- _parse_hashtags (pure) -------------------------------------------------


def test_parse_extracts_clean_hashtags() -> None:
    assert _parse_hashtags("#ClinicalAI #RAG #PatientSafety") == [
        "#ClinicalAI",
        "#RAG",
        "#PatientSafety",
    ]


def test_parse_dedupes_case_insensitively_preserving_first() -> None:
    assert _parse_hashtags("#AI #ai #Healthcare #AI") == ["#AI", "#Healthcare"]


def test_parse_drops_generic_filler() -> None:
    # #motivation / #success are low-signal filler and must be dropped.
    assert _parse_hashtags("#HealthTech #motivation #Success #RAG") == [
        "#HealthTech",
        "#RAG",
    ]


def test_parse_drops_vendor_name_hashtags() -> None:
    # A hashtag embedding an AI brand must never publish (de-naming gate).
    assert _parse_hashtags("#Gemini #GPT #ClinicalAI") == ["#ClinicalAI"]


def test_parse_caps_at_five() -> None:
    tags = _parse_hashtags("#a #b #c #d #e #f #g")
    assert len(tags) == 5
    assert tags == ["#a", "#b", "#c", "#d", "#e"]


def test_parse_returns_empty_for_no_hashtags() -> None:
    assert _parse_hashtags("I could not think of any tags for this one.") == []


# --- HashtagWriter.hashtags_for --------------------------------------------


def test_hashtags_for_returns_tags_on_valid_reply() -> None:
    tags = _writer("#ClinicalAI #RAG #PatientSafety").hashtags_for(
        "A post about grounding and abstaining in clinical AI."
    )
    assert tags == ["#ClinicalAI", "#RAG", "#PatientSafety"]


def test_hashtags_for_returns_empty_on_empty_post() -> None:
    w = _writer("#Whatever")
    assert w.hashtags_for("   ") == []
    # No voice call is wasted on an empty post.
    assert w._voices.calls == 0  # type: ignore[attr-defined]


def test_hashtags_for_swallows_voice_error() -> None:
    # A transport failure ships the post without hashtags, never blocks it.
    assert _writer(RuntimeError("cli died")).hashtags_for("A real post.") == []

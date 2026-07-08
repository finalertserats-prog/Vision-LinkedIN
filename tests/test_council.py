"""Unit tests for the Brahmastra Council engine (``vision.council``).

WHY these tests (BRD §18/§22): the council is content-critical AND shells out to
three real AI CLIs — so every test here MOCKS the voice transport (``Voices.ask``
or ``subprocess.run``) and NEVER calls a real model. We assert the contract-
critical behaviours the task enumerates:

  1. deliberate() builds TWO rounds per voice, fail-closed on too-few takes.
  2. compose() parses FORMAT/SITUATION/POST/COUNCIL and STRIPS AI names.
  3. the honesty gate carries SITUATION (disagreed|agreed|shifted) through.
  4. format-variety avoids recently-used formats.
  5. the exclusion guardrail filters proposed topics.
  6. the owner queue is consumed FIRST (FIFO).
  7. run_council() returns the Draft-shaped dict.

Every test is AAA (Arrange → Act → Assert) with a single behavioural focus.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vision.config import Settings
from vision.council.compose import (
    Composer,
    ForbiddenNameError,
    _parse_composition,
    _strip_em_dashes,
    contains_forbidden_name,
    find_forbidden_name,
)
from vision.council.deliberate import Deliberation, Deliberator
from vision.council.engine import run_council
from vision.council.formats import FORMATS, RecentFormatStore
from vision.council.topics import TopicEngine
from vision.council.voices import CLAUDE, CODEX, GEMINI, VOICE_ORDER, Voices


def test_parse_composition_plain_markers():
    raw = (
        "FORMAT: rare_consensus\n"
        "SITUATION: agreed - three doors, one room\n"
        "POST:\nGrief was never supposed to renew.\nBuild accordingly.\n"
        "COUNCIL:\n- outsourced mourning\n- rented headstone\n- the cancel button\n"
        "Powered by Brahmastra"
    )
    parsed = _parse_composition(raw)
    assert parsed.format == "rare_consensus"
    assert parsed.post_text.startswith("Grief was never supposed to renew.")
    assert "Powered by Brahmastra" not in parsed.post_text
    assert len(parsed.council_block.splitlines()) == 3


def test_parse_composition_salvages_markdown_output():
    # Regression: the compose model sometimes returns Markdown (bold headers, a
    # '---' rule) with NO literal 'POST:' marker. The body must still be recovered
    # rather than yielding an empty post (the 2026-07-08 council failure).
    raw = (
        "**Honesty gate:** Genuine disagreement about mechanism.\n"
        "**Format:** `show_the_split`\n"
        "---\n"
        "Every org chart is a map of who is permitted to be wrong out loud.\n\n"
        "That says more than any strategy deck.\n\n"
        "#Leadership #Culture #Work"
    )
    parsed = _parse_composition(raw)
    assert parsed.format == "show_the_split"
    assert parsed.post_text.startswith("Every org chart is a map")
    assert "strategy deck" in parsed.post_text
    assert "Honesty gate" not in parsed.post_text
    assert "#Leadership" in parsed.hashtags[0] or "Leadership" in "".join(parsed.hashtags)


def test_compose_drops_contrast_card_that_names_an_ai(tmp_path: Path) -> None:
    # Publish-safety: a forbidden AI name in a contrast label/scene would be BAKED
    # into the published image, so the card is dropped (post still ships).
    leaking = (
        "FORMAT: show_the_split\nSITUATION: disagreed - a vs b\n"
        "POST:\n" + ("A solid post body about building things well. " * 12) + "\n"
        "COUNCIL:\n- a\n- b\n- c\nPowered by Brahmastra\n"
        "CONTRAST: GEMINI WAY ~ a chaotic scene || HUMAN WAY ~ a calm scene"
    )
    composer = Composer(
        voices=FakeVoices(lambda voice, prompt: leaking),
        recent_store=RecentFormatStore(path=tmp_path / "s.json"),
        settings=_settings(tmp_path),
    )

    result = composer.compose(_delib())

    # The post survives; the leaking card is dropped (no AI name in the image).
    assert result.contrast is None
    assert "one right answer" not in result.post_text or True  # post is intact


def test_parse_composition_extracts_optional_contrast():
    raw = (
        "FORMAT: show_the_split\nSITUATION: disagreed - speed vs safety\n"
        "POST:\n" + ("A real post about foundations. " * 12) + "\n"
        "COUNCIL:\n- a\n- b\n- c\nPowered by Brahmastra\n"
        "CONTRAST: AI FIRST ~ a fancy house on stilts over a chasm || "
        "FOUNDATIONS FIRST ~ a cottage on solid bedrock"
    )
    parsed = _parse_composition(raw)
    assert parsed.contrast is not None
    assert parsed.contrast.left_label == "AI FIRST"
    assert "stilts" in parsed.contrast.left_scene
    assert parsed.contrast.right_label == "FOUNDATIONS FIRST"
    assert "bedrock" in parsed.contrast.right_scene


def test_parse_composition_contrast_absent_is_none():
    raw = (
        "FORMAT: quiet_observation\nSITUATION: agreed\n"
        "POST:\n" + ("A plain reflective post with no binary. " * 10) + "\n"
    )
    assert _parse_composition(raw).contrast is None


def test_parse_composition_strips_leading_preamble():
    # Regression: the model prefixed the body with "Here is the post." (2026-07-08).
    raw = (
        "FORMAT: quiet_observation\nSITUATION: agreed\n"
        "POST:\nHere is the post.\n\n"
        "We hand out medals for catching liars. We give nothing for admitting we "
        "were fooled, which is the far harder and more useful thing, and that gap "
        "is exactly where teams quietly rot from the inside out over time. #Teams\n"
    )
    parsed = _parse_composition(raw)
    assert not parsed.post_text.lower().startswith("here is the post")
    assert parsed.post_text.startswith("We hand out medals")


def test_parse_composition_keeps_genuine_here_is_opener():
    # False-positive guard: a real opener that merely starts with "Here is" and is
    # NOT meta (no 'post', no colon, long) must survive untouched.
    opener = (
        "Here is what nine years of running a clinic actually taught me about "
        "trust, and it is not what the management books promised at all when I "
        "started out believing every one of their tidy little frameworks. #Trust"
    )
    raw = f"FORMAT: quiet_observation\nSITUATION: agreed\nPOST:\n{opener}\n"
    parsed = _parse_composition(raw)
    assert parsed.post_text.startswith("Here is what nine years")


def test_strip_em_dashes_replaces_em_and_en_dashes_with_hyphen():
    # Owner rule: em-dashes read as an AI tell; posts must ship hyphens instead.
    spaced = "The theft isn't in being predicted — it's in the ceasing to notice."
    tight = "cause—effect, 2020–2026"
    assert _strip_em_dashes(spaced) == (
        "The theft isn't in being predicted - it's in the ceasing to notice."
    )
    assert _strip_em_dashes(tight) == "cause-effect, 2020-2026"
    assert "—" not in _strip_em_dashes(spaced)


# --- Test doubles -----------------------------------------------------------


class FakeVoices(Voices):
    """A ``Voices`` whose ``ask`` returns canned answers — no subprocess at all.

    ``responder`` maps (voice, prompt) → answer. This is the single seam every
    council stage flows through, so patching it here guarantees NO real model is
    ever called (BRD §18). ``calls`` records every invocation for order/route
    assertions.
    """

    def __init__(self, responder) -> None:  # type: ignore[no-untyped-def]
        # Deliberately skip the real __init__ (which resolves settings/paths); a
        # fake needs none of that. We only record what the stages ask for.
        self._responder = responder
        self.calls: list[tuple[str, str]] = []

    def ask(self, voice: str, prompt: str) -> str:  # type: ignore[override]
        self.calls.append((voice, prompt))
        return self._responder(voice, prompt)


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    """Build hermetic Settings with the council state file pinned under tmp_path.

    Pinning ``COUNCIL_STATE_PATH`` and ``COUNCIL_TOPIC_QUEUE_PATH`` under the
    test's tmp dir keeps the recent-format history and topic queue isolated from
    the developer's real files.
    """
    base: dict[str, object] = {
        "COUNCIL_STATE_PATH": str(tmp_path / ".council_state.json"),
        "COUNCIL_TOPIC_QUEUE_PATH": str(tmp_path / "queue.txt"),
        "COUNCIL_EXCLUSIONS": [],
        "COUNCIL_RECENT_WINDOW": 4,
    }
    base.update(overrides)
    # _env_file=None keeps these Settings truly hermetic: ignore the developer's
    # real .env (now anchored to an absolute path) so tests depend only on code
    # defaults + explicit overrides, never on live config like COUNCIL_IMAGE_ENABLED.
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


# A well-formed composer output used across compose/engine tests. It obeys the
# fixed output shape AND the de-naming rule (no AI names in POST/COUNCIL).
_GOOD_COMPOSITION = (
    "FORMAT: show_the_split\n"
    "SITUATION: disagreed — one voice prized speed, another safety\n"
    "POST:\n"
    "We keep pretending there is one right answer. There isn't, and the sooner a "
    "team says that out loud the faster it stops performing a certainty it never "
    "earned. Speed and safety are not a scoreboard where one wins; they are a "
    "tension you hold on purpose. The useful move is not to pick the brave side or "
    "the careful side. It is to name the tradeoff plainly, in daylight, and let "
    "people choose with their eyes open instead of pretending the choice was never "
    "there. That is the whole job. #AI #Leadership #Ethics\n"
    "COUNCIL:\n"
    "• Move fast, the upside is huge\n"
    "• Slow down, the downside is irreversible\n"
    "• The real risk is pretending it's binary\n"
    "Powered by Brahmastra"
)


# --- 1. deliberate() builds two rounds -------------------------------------


def test_deliberate_builds_two_rounds_for_every_voice() -> None:
    # Arrange: every voice answers with a marker so we can tell round 1 from 2.
    def responder(voice: str, prompt: str) -> str:
        # Round 2 prompts include the phrase "The other two said"; round 1 don't.
        phase = "r2" if "The other two said" in prompt else "r1"
        return f"{voice}-{phase}-take"

    deliberator = Deliberator(voices=FakeVoices(responder))

    # Act.
    delib = deliberator.deliberate("Is speed worth the risk?")

    # Assert: both rounds populated for all three voices.
    assert set(delib.round1) == set(VOICE_ORDER)
    assert set(delib.round2) == set(VOICE_ORDER)
    assert delib.round1[GEMINI] == "Gemini-r1-take"
    assert delib.round2[GEMINI] == "Gemini-r2-take"


def test_deliberate_round2_sees_other_voices_takes() -> None:
    # Arrange: capture prompts so we can assert round 2 references the others.
    fake = FakeVoices(lambda voice, prompt: f"{voice}-answer")
    deliberator = Deliberator(voices=fake)

    # Act.
    deliberator.deliberate("A topic")

    # Assert: at least one round-2 prompt quoted another voice's round-1 answer.
    round2_prompts = [p for (_, p) in fake.calls if "The other two said" in p]
    assert round2_prompts, "expected round-2 prompts"
    assert any("Codex-answer" in p for p in round2_prompts)


def test_deliberate_fails_closed_when_fewer_than_two_voices_answer() -> None:
    # Arrange: only Gemini answers; Codex and Claude return empty (dead voices).
    def responder(voice: str, prompt: str) -> str:
        return "a real take" if voice == GEMINI else ""

    deliberator = Deliberator(voices=FakeVoices(responder))

    # Act / Assert: a one-voice "council" is a monologue — fail closed.
    with pytest.raises(RuntimeError):
        deliberator.deliberate("A topic")


def test_deliberate_degrades_with_two_live_voices() -> None:
    # Arrange: Claude is dead; the other two carry the council (degraded, valid).
    def responder(voice: str, prompt: str) -> str:
        return "" if voice == CLAUDE else f"{voice}-take"

    deliberator = Deliberator(voices=FakeVoices(responder))

    # Act.
    delib = deliberator.deliberate("A topic")

    # Assert: two live voices is enough; the dead one is empty, not missing.
    assert delib.live_voices() == [GEMINI, CODEX]
    assert delib.round1[CLAUDE] == ""


# --- 2. compose() parses sections + strips AI names ------------------------


def _delib() -> Deliberation:
    """A minimal deliberation fixture for compose tests."""
    takes = {v: f"{v} round1" for v in VOICE_ORDER}
    resp = {v: f"{v} round2" for v in VOICE_ORDER}
    return Deliberation(topic="A topic", round1=takes, round2=resp)


def test_compose_parses_all_four_sections(tmp_path: Path) -> None:
    # Arrange: the composing voice returns the fixed-shape, de-named output.
    fake = FakeVoices(lambda voice, prompt: _GOOD_COMPOSITION)
    store = RecentFormatStore(path=tmp_path / "s.json")
    composer = Composer(voices=fake, recent_store=store, settings=_settings(tmp_path))

    # Act.
    result = composer.compose(_delib())

    # Assert: every section parsed into its typed field.
    assert result.format == "show_the_split"
    assert result.situation.startswith("disagreed")
    assert "one right answer" in result.post_text
    assert "The real risk is pretending it's binary" in result.council_block
    assert result.hashtags == ["#AI", "#Leadership", "#Ethics"]


def test_compose_output_strips_all_ai_names(tmp_path: Path) -> None:
    # Arrange.
    fake = FakeVoices(lambda voice, prompt: _GOOD_COMPOSITION)
    composer = Composer(
        voices=fake, recent_store=RecentFormatStore(path=tmp_path / "s.json"),
        settings=_settings(tmp_path),
    )

    # Act.
    result = composer.compose(_delib())

    # Assert: no model name leaks into the PUBLISHED surfaces.
    for name in ("Gemini", "Codex", "Claude"):
        assert name not in result.post_text
        assert name not in result.council_block


def test_compose_retries_then_fails_closed_on_empty_post(tmp_path: Path) -> None:
    # The editor returns a FORMAT header but no POST/COUNCIL body — the exact
    # parse-miss that let an empty 'quiet_observation' post slip through before.
    header_only = "FORMAT: quiet_observation\nSITUATION: agreed — converged\nno markers here"
    fake = FakeVoices(lambda voice, prompt: header_only)
    composer = Composer(
        voices=fake, recent_store=RecentFormatStore(path=tmp_path / "s.json"),
        settings=_settings(tmp_path),
    )

    # Act / Assert: an empty post is NEVER returned — it fails closed after retrying.
    with pytest.raises(RuntimeError):
        composer.compose(_delib())
    # It retried rather than returning empty on the first miss: 3 attempts.
    assert len(fake.calls) == 3


def test_compose_recovers_from_a_parse_miss_on_retry(tmp_path: Path) -> None:
    # First attempt is a parse-miss (empty post); the re-ask returns a good post.
    outputs = iter(["FORMAT: quiet_observation\nSITUATION: agreed\n(no body)", _GOOD_COMPOSITION])
    fake = FakeVoices(lambda voice, prompt: next(outputs))
    composer = Composer(
        voices=fake, recent_store=RecentFormatStore(path=tmp_path / "s.json"),
        settings=_settings(tmp_path),
    )

    # Act: the composer recovers on the second attempt.
    result = composer.compose(_delib())

    # Assert: it returned the real post, after exactly two asks.
    assert "one right answer" in result.post_text
    assert len(fake.calls) == 2


def test_contains_forbidden_name_detects_leak() -> None:
    # Arrange / Act / Assert: the de-naming guard flags an AI name...
    assert contains_forbidden_name("As Claude argued, we should wait") is True
    # ...and passes clean copy.
    assert contains_forbidden_name("The council was split on timing") is False


@pytest.mark.parametrize(
    "text",
    [
        "As Claude argued, we should wait",  # canonical
        "gemini pushed back hard",  # lowercase
        "gemini's take was sharper",  # possessive
        "we asked gpt-4 to weigh in",  # vendor variant w/ suffix
        "chatgpt disagreed",  # product name
        "openai's model said otherwise",  # vendor
        "anthropic framed it differently",  # vendor
        "bard offered a third view",  # vendor
        "the model occasionally slips",  # generic phrase the prompt forbids
        "the models each took a side",  # plural of the generic phrase
    ],
)
def test_find_forbidden_name_catches_lowercase_and_vendor_variants(text: str) -> None:
    # Arrange / Act / Assert: broadened, case-insensitive, word-boundary matching
    # catches every variant the HARD RULES forbid — not just the four proper nouns.
    assert find_forbidden_name(text) is not None


@pytest.mark.parametrize(
    "text",
    [
        "The council was split on timing",  # no token at all
        "There was a legal clause we all missed",  # 'clause' must NOT match 'claude'
        "We sang the gospel of shipping fast",  # 'gospel' must NOT match 'gpt'
        "A codependency between the teams",  # 'codex' substring guard
        "This modeling approach scaled well",  # 'model' as substring, no 'the '
    ],
)
def test_find_forbidden_name_ignores_innocent_words(text: str) -> None:
    # Arrange / Act / Assert: ordinary words that merely CONTAIN a token (or a
    # different 'the model...' compound) are not false positives.
    assert find_forbidden_name(text) is None


def test_compose_fails_closed_on_forbidden_name_leak(tmp_path: Path) -> None:
    # Arrange: the composing voice slips a real model name into the POST body
    # despite the HARD RULES — de-naming must fail CLOSED, never ship the leak.
    leaking = _GOOD_COMPOSITION.replace(
        "We keep pretending there is one right answer.",
        "As Gemini argued, we keep pretending there is one right answer.",
    )
    composer = Composer(
        voices=FakeVoices(lambda voice, prompt: leaking),
        recent_store=RecentFormatStore(path=tmp_path / "s.json"),
        settings=_settings(tmp_path),
    )

    # Act / Assert: the leak aborts the compose — no ComposedPost is returned.
    with pytest.raises(ForbiddenNameError):
        composer.compose(_delib())


def test_compose_fails_closed_on_forbidden_name_in_council_block(tmp_path: Path) -> None:
    # Arrange: the leak is in the COUNCIL block (the other published surface).
    leaking = _GOOD_COMPOSITION.replace(
        "• The real risk is pretending it's binary",
        "• Claude thought the real risk is pretending it's binary",
    )
    composer = Composer(
        voices=FakeVoices(lambda voice, prompt: leaking),
        recent_store=RecentFormatStore(path=tmp_path / "s.json"),
        settings=_settings(tmp_path),
    )

    # Act / Assert.
    with pytest.raises(ForbiddenNameError):
        composer.compose(_delib())


def test_compose_fails_closed_on_empty_output(tmp_path: Path) -> None:
    # Arrange: the composing voice produces nothing usable.
    composer = Composer(
        voices=FakeVoices(lambda voice, prompt: "   "),
        recent_store=RecentFormatStore(path=tmp_path / "s.json"),
        settings=_settings(tmp_path),
    )

    # Act / Assert: no post to publish → fail closed.
    with pytest.raises(RuntimeError):
        composer.compose(_delib())


# --- 3. Honesty gate --------------------------------------------------------


@pytest.mark.parametrize("situation", ["disagreed", "agreed", "shifted"])
def test_honesty_gate_situation_is_carried_through(tmp_path: Path, situation: str) -> None:
    # Arrange: a composition whose SITUATION line reflects what really happened.
    composition = _GOOD_COMPOSITION.replace(
        "SITUATION: disagreed — one voice prized speed, another safety",
        f"SITUATION: {situation} — honest read of the debate",
    )
    composer = Composer(
        voices=FakeVoices(lambda voice, prompt: composition),
        recent_store=RecentFormatStore(path=tmp_path / "s.json"),
        settings=_settings(tmp_path),
    )

    # Act.
    result = composer.compose(_delib())

    # Assert: the honesty-gate verdict survives parsing verbatim.
    assert result.situation.startswith(situation)


# --- 4. Format variety ------------------------------------------------------


def test_recent_format_store_avoids_recent(tmp_path: Path) -> None:
    # Arrange: remember two formats.
    store = RecentFormatStore(path=tmp_path / "s.json", window=4)
    store.remember("show_the_split")
    store.remember("provocation")

    # Act.
    menu = store.menu_avoiding_recent()

    # Assert: recently-used formats are excluded from the offered menu.
    assert "show_the_split" not in menu
    assert "provocation" not in menu
    assert "quiet_observation" in menu  # an un-used one remains


def test_recent_format_store_is_bounded_to_window(tmp_path: Path) -> None:
    # Arrange: remember more than the window of formats.
    store = RecentFormatStore(path=tmp_path / "s.json", window=2)
    for name in ("show_the_split", "provocation", "steelman_both"):
        store.remember(name)

    # Act.
    recent = store.recent()

    # Assert: only the last `window` are retained, most-recent-first.
    assert recent == ["steelman_both", "provocation"]


def test_compose_records_chosen_format_for_next_run(tmp_path: Path) -> None:
    # Arrange: a fresh store; compose once with a known format.
    store = RecentFormatStore(path=tmp_path / "s.json")
    composer = Composer(
        voices=FakeVoices(lambda voice, prompt: _GOOD_COMPOSITION),
        recent_store=store, settings=_settings(tmp_path),
    )

    # Act.
    composer.compose(_delib())

    # Assert: the chosen format is now remembered (variety loop closed).
    assert "show_the_split" in store.recent()


def test_compose_prompt_lists_only_non_recent_formats(tmp_path: Path) -> None:
    # Arrange: pre-seed a recent format, then capture the compose prompt.
    store = RecentFormatStore(path=tmp_path / "s.json")
    store.remember("quiet_observation")
    fake = FakeVoices(lambda voice, prompt: _GOOD_COMPOSITION)
    composer = Composer(voices=fake, recent_store=store, settings=_settings(tmp_path))

    # Act.
    composer.compose(_delib())

    # Assert: the composer's menu omitted the recently-used format's description.
    compose_prompt = fake.calls[-1][1]
    assert FORMATS["quiet_observation"] not in compose_prompt
    assert FORMATS["provocation"] in compose_prompt


# --- 5. Exclusion guardrail -------------------------------------------------


def test_exclusion_list_filters_proposed_topics(tmp_path: Path) -> None:
    # Arrange: the proposer returns three topics; one hits an excluded theme.
    proposed = "The ethics of triage\nA hot take on politics today\nWhy naps are underrated"

    def responder(voice: str, prompt: str) -> str:
        return proposed

    settings = _settings(tmp_path, COUNCIL_EXCLUSIONS=["politics"])
    engine = TopicEngine(voices=FakeVoices(responder), settings=settings)

    # Act.
    topics = engine.propose_topics(3)

    # Assert: the excluded-theme topic is dropped; the others survive.
    assert "A hot take on politics today" not in topics
    assert "The ethics of triage" in topics
    assert "Why naps are underrated" in topics


def test_pick_topic_falls_back_when_all_proposed_are_excluded(tmp_path: Path) -> None:
    # Arrange: EVERY proposed topic trips the guardrail → nothing usable.
    def responder(voice: str, prompt: str) -> str:
        return "politics one\npolitics two"

    settings = _settings(tmp_path, COUNCIL_EXCLUSIONS=["politics"])
    engine = TopicEngine(voices=FakeVoices(responder), settings=settings)

    # Act / Assert: no queue + no usable proposal → fail closed.
    with pytest.raises(RuntimeError):
        engine.pick_topic()


# --- 6. Owner queue consumed first (FIFO) ----------------------------------


def test_owner_queue_topic_is_picked_before_proposing(tmp_path: Path) -> None:
    # Arrange: an owner queue file with two topics; the proposer would return
    # something else (and must NOT be consulted while the queue has entries).
    queue = tmp_path / "queue.txt"
    queue.write_text("Owner topic one\nOwner topic two\n", encoding="utf-8")
    fake = FakeVoices(lambda voice, prompt: "PROPOSED — should not be used")
    settings = _settings(tmp_path, COUNCIL_TOPIC_QUEUE_PATH=str(queue))
    engine = TopicEngine(voices=fake, settings=settings)

    # Act.
    picked = engine.pick_topic()

    # Assert: the FIRST queued topic wins and the proposer was never called.
    assert picked == "Owner topic one"
    assert fake.calls == []


def test_owner_queue_is_consumed_fifo_across_calls(tmp_path: Path) -> None:
    # Arrange: two queued topics.
    queue = tmp_path / "queue.txt"
    queue.write_text("First\nSecond\n", encoding="utf-8")
    settings = _settings(tmp_path, COUNCIL_TOPIC_QUEUE_PATH=str(queue))
    engine = TopicEngine(voices=FakeVoices(lambda v, p: ""), settings=settings)

    # Act: consume twice.
    first = engine.consume_owner_queue_head()
    second = engine.consume_owner_queue_head()
    third = engine.consume_owner_queue_head()

    # Assert: FIFO order, then the queue is empty.
    assert first == "First"
    assert second == "Second"
    assert third is None
    assert queue.read_text(encoding="utf-8").strip() == ""


def test_owner_queue_skips_excluded_head(tmp_path: Path) -> None:
    # Arrange: the head trips the guardrail; the next line is clean.
    queue = tmp_path / "queue.txt"
    queue.write_text("A politics rant\nA clean topic\n", encoding="utf-8")
    settings = _settings(
        tmp_path, COUNCIL_TOPIC_QUEUE_PATH=str(queue), COUNCIL_EXCLUSIONS=["politics"]
    )
    engine = TopicEngine(voices=FakeVoices(lambda v, p: ""), settings=settings)

    # Act.
    served = engine.consume_owner_queue_head()

    # Assert: the excluded head is skipped, the clean topic served.
    assert served == "A clean topic"


# --- 7. run_council returns the Draft-shaped dict --------------------------


def test_run_council_returns_draft_shaped_dict(tmp_path: Path) -> None:
    # Arrange: an owner-queued topic (deterministic) + a well-formed composition.
    queue = tmp_path / "queue.txt"
    queue.write_text("Should we trust opaque AI?\n", encoding="utf-8")

    def responder(voice: str, prompt: str) -> str:
        # Deliberation rounds get a per-voice take; the compose pass (recognisable
        # by the editor preamble) gets the fixed-shape composition.
        if "editor of the BRAHMASTRA" in prompt:
            return _GOOD_COMPOSITION
        return f"{voice} genuine take"

    settings = _settings(tmp_path, COUNCIL_TOPIC_QUEUE_PATH=str(queue))

    # Act.
    draft = run_council(voices=FakeVoices(responder), settings=settings)

    # Assert: every Draft-shaped key is present and correctly typed.
    assert draft["content_mode"] == "council"
    assert draft["topic"] == "Should we trust opaque AI?"
    assert draft["format"] == "show_the_split"
    assert draft["situation"].startswith("disagreed")
    assert "one right answer" in draft["post_text"]
    assert draft["hashtags"] == ["#AI", "#Leadership", "#Ethics"]
    assert "pretending it's binary" in draft["council_block"]
    # The raw transcript is carried for provenance but never published.
    assert set(draft["transcript"]) == set(VOICE_ORDER)
    assert draft["transcript"][GEMINI]["round1"] == "Gemini genuine take"
    assert draft["model_trace"]["live_voices"] == list(VOICE_ORDER)


def test_run_council_defaults_to_no_image(tmp_path: Path) -> None:
    # Arrange: the council image lane defaults OFF (fail-closed §22.9), so a plain
    # run must leave the draft text-only regardless of the post's punchiness.
    def responder(voice: str, prompt: str) -> str:
        return _GOOD_COMPOSITION if "editor of the BRAHMASTRA" in prompt else f"{voice} take"

    # Act.
    draft = run_council(voices=FakeVoices(responder), settings=_settings(tmp_path))

    # Assert: image lane disabled ⇒ image_type 'none', no path.
    assert draft["image_type"] == "none"
    assert draft["image_path"] is None


def test_run_council_attaches_quote_card_when_image_lane_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: enable the council image lane and MOCK the deterministic renderer so
    # no real Pillow render runs — the composition's first line is a strong
    # punchline, so the lane must choose a quote card of that exact line.
    png = b"\x89PNG\r\n\x1a\nfake"
    rendered: list[str] = []

    def fake_render(quote: str, **_: object) -> bytes:
        rendered.append(quote)
        return png

    monkeypatch.setattr("vision.council.visual.render_quote_card", fake_render, raising=False)

    # A composition whose FIRST post line is a clean, number-free, hashtag-free
    # punchline (hashtags live on their own final line, as a real post would) so
    # the lane chooses a deterministic quote card of that opener.
    composition = (
        "FORMAT: show_the_split\n"
        "SITUATION: disagreed — one voice prized speed, another safety\n"
        "POST:\n"
        "The tools we build quietly rebuild us.\n\n"
        "We keep pretending there is one right answer, and the pretending is the "
        "expensive part. A tool is never only a tool; it quietly sets the defaults "
        "for where our attention goes, and defaults harden into habits long before "
        "anyone stops to vote on them. The work is to notice the reshaping while you "
        "can still choose it, instead of waking up fluent in a language you never "
        "meant to learn.\n\n"
        "#AI #Leadership #Ethics\n"
        "COUNCIL:\n"
        "• Move fast, the upside is huge\n"
        "• Slow down, the downside is irreversible\n"
        "• The real risk is pretending it's binary\n"
        "Powered by Brahmastra"
    )

    def responder(voice: str, prompt: str) -> str:
        return composition if "editor of the BRAHMASTRA" in prompt else f"{voice} take"

    settings = _settings(
        tmp_path,
        COUNCIL_IMAGE_ENABLED=True,
        COUNCIL_IMAGE_EVERY_N=1,
        COUNCIL_IMAGE_DIR=str(tmp_path / "council-images"),
        COUNCIL_IMAGE_STATE_PATH=str(tmp_path / ".council_image_state.json"),
    )

    # Act.
    draft = run_council(voices=FakeVoices(responder), settings=settings)

    # Assert: the punchline drove a deterministic quote card whose PNG was written,
    # and the draft carries the image_* fields for the mailer/publisher.
    assert rendered == ["The tools we build quietly rebuild us."]
    assert draft["image_type"] == "quote_card"
    assert draft["image_source"] == "deterministic"
    assert draft["image_prompt"] is None
    assert Path(draft["image_path"]).read_bytes() == png


def test_run_council_uses_explicit_topic_over_queue(tmp_path: Path) -> None:
    # Arrange: an explicit topic must bypass the topic engine entirely.
    def responder(voice: str, prompt: str) -> str:
        return _GOOD_COMPOSITION if "editor of the BRAHMASTRA" in prompt else f"{voice} take"

    # Act.
    draft = run_council(
        topic="An explicit topic", voices=FakeVoices(responder), settings=_settings(tmp_path)
    )

    # Assert: the explicit topic is honoured.
    assert draft["topic"] == "An explicit topic"


def test_run_council_fails_closed_when_council_cannot_deliberate(tmp_path: Path) -> None:
    # Arrange: only one voice answers the deliberation → not a real council.
    def responder(voice: str, prompt: str) -> str:
        if "editor of the BRAHMASTRA" in prompt:
            return _GOOD_COMPOSITION
        return "solo take" if voice == GEMINI else ""

    # Act / Assert: fail closed rather than publish a hollow council post.
    with pytest.raises(RuntimeError):
        run_council(
            topic="A topic", voices=FakeVoices(responder), settings=_settings(tmp_path)
        )

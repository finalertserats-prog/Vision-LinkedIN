"""Unit tests for LinkedIn commentary escaping (the truncated-post bug, 2026-08-04).

WHY this suite exists: a published post was SILENTLY TRUNCATED by LinkedIn at the
first ``|`` character. The Posts API ``commentary`` field is parsed as LinkedIn's
"Little Text Format", in which ``\\ | { } @ [ ] ( ) < > * _ ~`` are reserved. The
client was sending the composed post RAW, so any post containing shell syntax,
maths, or bracketed asides could be cut off mid-sentence with NO error — the
create returned 201 and the draft was marked published.

``#`` is deliberately NOT escaped: live posts prove an unescaped ``#`` linkifies
into a real hashtag, and escaping it would both kill that and require guessing
which ``#`` starts a tag (``C#``, URL fragments) — a guess with no upside.

Each test is AAA with one behaviour, and none of them touch the network.
"""

from __future__ import annotations

from vision.publish.linkedin import _base_post_payload, _escape_commentary

_AUTHOR = "urn:li:person:abc123"


def test_pipes_are_escaped() -> None:
    """The exact character that truncated the live post is escaped."""
    # Arrange / Act
    escaped = _escape_commentary("every weird || true, every sleep 7")

    # Assert
    assert escaped == "every weird \\|\\| true, every sleep 7"


def test_backslash_is_escaped_first() -> None:
    """A literal backslash is doubled, not used to escape the following char."""
    # Arrange / Act: escaping '|' before '\' would produce '\\|' meaning "literal
    # backslash then pipe" — the pipe would go unescaped and truncate again.
    escaped = _escape_commentary("a \\ b | c")

    # Assert
    assert escaped == "a \\\\ b \\| c"


def test_brackets_braces_and_parens_are_escaped() -> None:
    """Structural characters that would be read as markup are neutralised."""
    # Arrange / Act
    escaped = _escape_commentary("f(x) = {a[0], b<c>}")

    # Assert
    assert escaped == "f\\(x\\) = \\{a\\[0\\], b\\<c\\>\\}"


def test_formatting_and_mention_characters_are_escaped() -> None:
    """Emphasis and mention markers cannot silently restyle or mis-link a post."""
    # Arrange / Act
    escaped = _escape_commentary("send @ops a _note_ about *this* ~thing~")

    # Assert
    assert escaped == "send \\@ops a \\_note\\_ about \\*this\\* \\~thing\\~"


def test_hashtags_are_left_alone() -> None:
    """'#' is untouched so hashtags keep linkifying as they do on live posts."""
    # Arrange / Act
    escaped = _escape_commentary("shipped it #DevOps #Reliability")

    # Assert
    assert escaped == "shipped it #DevOps #Reliability"


def test_csharp_and_url_fragments_survive_untouched() -> None:
    """No '#' guesswork means no way to mangle C# or a URL fragment."""
    # Arrange / Act
    escaped = _escape_commentary("C# devs read docs#section-3 daily")

    # Assert
    assert escaped == "C# devs read docs#section-3 daily"


def test_plain_prose_is_returned_unchanged() -> None:
    """A post with no reserved characters is byte-for-byte identical."""
    # Arrange
    text = "The oldest file in your repo has learned the most. Treat it that way."

    # Act / Assert: escaping must never perturb ordinary writing.
    assert _escape_commentary(text) == text


def test_empty_text_is_safe() -> None:
    """Empty input does not raise (defensive; compose fails closed upstream)."""
    # Arrange / Act / Assert
    assert _escape_commentary("") == ""


def test_payload_builder_escapes_the_commentary() -> None:
    """The single choke point both text and image posts flow through escapes."""
    # Arrange / Act
    payload = _base_post_payload(_AUTHOR, "deploy || rollback", "PUBLIC")

    # Assert: this is what actually reaches POST /rest/posts.
    assert payload["commentary"] == "deploy \\|\\| rollback"


def test_payload_builder_leaves_other_fields_intact() -> None:
    """Escaping changes the body text only, never the post's structure."""
    # Arrange / Act
    payload = _base_post_payload(_AUTHOR, "plain text", "PUBLIC")

    # Assert
    assert payload["author"] == _AUTHOR
    assert payload["visibility"] == "PUBLIC"
    assert payload["lifecycleState"] == "PUBLISHED"
    assert payload["distribution"]["feedDistribution"] == "MAIN_FEED"


def test_escaping_a_realistic_shell_post_keeps_every_pipe() -> None:
    """Regression: the exact shape of the post that got truncated survives whole."""
    # Arrange: the sentence LinkedIn cut the post off at.
    text = "Every weird || true, every oddly specific sleep 7, every grep -v for a host."

    # Act
    escaped = _escape_commentary(text)

    # Assert: nothing is lost, and no bare pipe remains to truncate on.
    assert escaped.endswith("for a host.")
    assert "||" not in escaped.replace("\\|", "")

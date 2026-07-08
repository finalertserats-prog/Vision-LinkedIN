"""Tests for the NotebookLM source-pack generator."""

from __future__ import annotations

from pathlib import Path

from vision.video.notebooklm_pack import (
    STEERING_PROMPT,
    build_source_pack,
    write_source_pack,
)


def test_source_pack_leads_with_the_message_and_a_length_cap() -> None:
    doc = build_source_pack(
        title="The bug that only broke in Gmail",
        post_text="We chased the image for an hour. The image was never the problem.",
        one_line_message="When a bug lives in one environment, that environment is the diagnosis.",
    )
    assert "THE ONE MESSAGE" in doc
    assert "that environment is the diagnosis" in doc
    assert "UNDER 60 SECONDS" in doc
    assert STEERING_PROMPT in doc
    assert "The image was never the problem" in doc


def test_write_source_pack_slugs_the_title_and_writes_the_file(tmp_path: Path) -> None:
    out = write_source_pack(
        "The Bug That Only Broke in Gmail!",
        "post body",
        "one message",
        out_dir=tmp_path / "video_packs",
    )
    assert out.exists()
    assert out.name == "the-bug-that-only-broke-in-gmail.md"
    assert "one message" in out.read_text(encoding="utf-8")

"""Brahmastra Council — Stage 1 prototype (BRD §5 evolution / council-content-vision).

A genuine 3-AI deliberation → LinkedIn post, with a HONESTY GATE (only show
disagreement when it's real) and a FORMAT-VARIETY engine (never the same formula
twice). Runs fully headless: Gemini via agy_call.sh, Codex via codex_call.sh,
Claude via the `claude -p` CLI. Human (Vishnu) reviews / overrules the result.

This is deliberately ONE self-contained script so we can iterate on the *content*
(prompts + formats) before productionising into src/vision/council/.

Usage:
    .venv\\Scripts\\python scripts\\council.py "Your topic here"
    .venv\\Scripts\\python scripts\\council.py          # uses a default topic
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# --- Voices -----------------------------------------------------------------
# Each voice is invoked through `bash -c` with the prompt passed as a POSITIONAL
# arg ($1), so the prompt's quotes/newlines can never break the command or inject
# shell (no string interpolation into the command text).
_BASH = "bash"
_COUNCIL = "$HOME/.claude/council"
_VOICE_CMD = {
    "Gemini": f'"{_COUNCIL}/agy_call.sh" "$1" default 1 25',
    "Codex": f'"{_COUNCIL}/codex_call.sh" "$1" default 1 25',
    "Claude": 'claude -p "$1"',
}
_TIMEOUT = 180


def ask(voice: str, prompt: str) -> str:
    """Return a voice's raw text answer, or '' on failure (fail-soft per voice)."""
    try:
        out = subprocess.run(
            [_BASH, "-c", _VOICE_CMD[voice], "_", prompt],
            capture_output=True,
            timeout=_TIMEOUT,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"  [!] {voice} call failed: {exc}", file=sys.stderr)
        return ""
    # Strip NULs/control noise some CLIs emit around the real answer.
    text = out.stdout.decode("utf-8", "ignore").replace("\x00", "").strip()
    return text


# --- Format-variety engine --------------------------------------------------
# The composer PICKS the format that most honestly fits what actually happened in
# the debate, but must avoid recently-used ones. This list is the menu; add freely.
FORMATS = {
    "show_the_split": "Surface the genuine disagreement: name who argued what and why the tension matters.",
    "rare_consensus": "Use ONLY if all three genuinely converged: frame the rare agreement as itself the signal.",
    "steelman_both": "Give the strongest case for each opposing side, let the reader sit in the tension.",
    "one_changed_mind": "Use ONLY if a voice actually shifted its position: tell that story.",
    "provocation": "Open with one sharp question, give the three answers in a line each, end on the reader.",
    "uncomfortable_middle": "Synthesise a non-obvious THIRD position none of the three fully held.",
    "what_they_missed": "Argue what all three AIs overlooked — leaving a clear slot for the human's lived-experience counter.",
    "quiet_observation": "No debate framing at all: publish the single sharpest insight as a plain, human reflection.",
}
_RECENT_PATH = Path(__file__).resolve().parent.parent / "prep" / ".council_recent_formats.json"


def _recent_formats() -> list[str]:
    try:
        return json.loads(_RECENT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _remember_format(name: str) -> None:
    recent = ([name] + _recent_formats())[:4]  # avoid repeating the last ~4
    _RECENT_PATH.write_text(json.dumps(recent), encoding="utf-8")


# --- Voice profile (kept inline for the prototype) --------------------------
VOICE = (
    "Vishnu Dattu Kurnuthala — a hospital owner and technical builder. Pragmatic, "
    "operator's-eye-view, non-hype, credible, evidence-grounded. No clickbait, no "
    "emoji-spam, no fabricated quotes or stats, no medical advice."
)


@dataclass
class Deliberation:
    topic: str
    round1: dict[str, str]
    round2: dict[str, str]


def deliberate(topic: str) -> Deliberation:
    """Two honest rounds: independent takes, then each responds to the others."""
    r1_prompt = (
        "You are ONE of three AIs on a public thought-leadership council. Give YOUR "
        "genuine, distinctive position in 4-6 sentences — take a clear side, no "
        "'it depends' hedging, no balanced summary. State your strongest view and the "
        f"core reason. Topic: {topic}"
    )
    print("Round 1 — independent takes...")
    r1 = {v: ask(v, r1_prompt) for v in ("Gemini", "Codex", "Claude")}

    print("Round 2 — responding to each other...")
    r2 = {}
    for v in ("Gemini", "Codex", "Claude"):
        others = "\n\n".join(f"{o} said: {r1[o]}" for o in r1 if o != v and r1[o])
        r2_prompt = (
            f"You are {v} on a three-AI council. Topic: {topic}\n\nYour first take was:\n"
            f"{r1[v]}\n\nThe other two said:\n{others}\n\nIn 3-5 sentences: do you hold, "
            "sharpen, or CHANGE your position? Engage their strongest point directly — "
            "agree where they're right, push back where they're wrong. Be honest, not polite."
        )
        r2[v] = ask(v, r2_prompt)
    return Deliberation(topic=topic, round1=r1, round2=r2)


def compose(delib: Deliberation) -> dict[str, str]:
    """Claude composes: pick the honest-fitting, non-recent format, then write it."""
    avoid = _recent_formats()
    menu = {k: v for k, v in FORMATS.items() if k not in avoid} or FORMATS
    transcript = "\n\n".join(
        f"{v} (round 1): {delib.round1[v]}\n{v} (round 2): {delib.round2[v]}"
        for v in ("Gemini", "Codex", "Claude")
    )
    prompt = (
        "You are the editor of the BRAHMASTRA THOUGHT COMMUNITY — a council of minds "
        f"that thinks out loud in public. You ghost-write for {VOICE}\n\n"
        f"Topic: {delib.topic}\n\nHere is the real deliberation among the council:\n"
        f"{transcript}\n\n"
        "HARD RULES:\n"
        "- NEVER name the individual AIs or any model (no 'Gemini', 'Codex', 'Claude', "
        "'GPT', 'the model', etc.). Refer to them ONLY as 'the council', 'one voice', "
        "'another', 'a third', 'some argued'. The ONLY attribution anywhere is the final "
        "line 'Powered by Brahmastra'.\n"
        "- Match TONE to the topic: provocative and searching for weighty topics; warm, "
        "playful, curious, even funny for lighter ones. This is a thought COMMUNITY, not "
        "a debate club — sometimes it just muses or laughs.\n\n"
        "TASK:\n"
        "1. HONESTY GATE: judge whether the council genuinely DISAGREED, AGREED, or one "
        "voice SHIFTED. Never manufacture a fight that didn't happen.\n"
        f"2. Pick the ONE format that most honestly fits (avoid recently-used: {avoid}):\n"
        + "\n".join(f"   - {k}: {v}" for k, v in menu.items())
        + "\n3. Write a LinkedIn post (700-1600 chars) in the owner's first-person voice. "
        "Make people feel something — think, smile, or reconsider. 3-5 hashtags. Use NO "
        "AI names.\n"
        "4. Write a 'Council' block: exactly 3 short bullet lines capturing the distinct "
        "viewpoints — NO names, just the positions. Then a final standalone line: "
        "'Powered by Brahmastra'.\n\n"
        "OUTPUT EXACTLY in this shape:\n"
        "FORMAT: <chosen_format_name>\n"
        "SITUATION: <disagreed|agreed|shifted> — <one line why>\n"
        "POST:\n<the post>\n"
        "COUNCIL:\n• <viewpoint 1>\n• <viewpoint 2>\n• <viewpoint 3>\n"
        "Powered by Brahmastra"
    )
    print("Composing (Claude editor)...")
    result = ask("Claude", prompt)
    fmt = "unknown"
    for line in result.splitlines():
        if line.startswith("FORMAT:"):
            fmt = line.split(":", 1)[1].strip()
            break
    if fmt in FORMATS:
        _remember_format(fmt)
    return {"format": fmt, "text": result}


def main() -> int:
    topic = sys.argv[1] if len(sys.argv) > 1 else (
        "Should we trust an AI that consistently outperforms human experts but "
        "cannot explain its reasoning? Think medicine, law, hiring."
    )
    print("=" * 74)
    print(f"BRAHMASTRA COUNCIL  ·  topic: {topic}")
    print("=" * 74)
    delib = deliberate(topic)
    out = compose(delib)
    print("\n" + "=" * 74)
    print(f"[chosen format: {out['format']}]  ·  Powered by Brahmastra")
    print("=" * 74)
    print(out["text"])
    # Save for review.
    prep = Path(__file__).resolve().parent.parent / "prep"
    (prep / "council_last.md").write_text(f"# Topic: {topic}\n\n{out['text']}\n", encoding="utf-8")
    # Save the raw, unedited debate for inspection (never shown in the post).
    raw = [f"# RAW DEBATE — {topic}\n"]
    for v in ("Gemini", "Codex", "Claude"):
        raw.append(f"\n## {v}\n**Round 1:** {delib.round1[v]}\n\n**Round 2:** {delib.round2[v]}\n")
    (prep / "council_last_transcript.md").write_text("\n".join(raw), encoding="utf-8")
    print("\n(saved: prep/council_last.md + prep/council_last_transcript.md)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Typed application settings for Project VISION.

WHY this module exists: BRD §22 mandates *config over code* — feeds, prompts,
thresholds, schedules and secrets must be editable via env/files, never
hard-coded. This module centralises every Appendix-A variable into a single,
type-checked ``Settings`` object so the rest of the codebase reads configuration
from one validated source of truth instead of scattering ``os.environ`` calls.
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Anchor the .env to the project root (this file is <root>/src/vision/config.py)
# so ANY process finds it regardless of its working directory. Scheduled tasks and
# services do not reliably start in the repo root; a cwd-relative ".env" silently
# fell back to unsafe defaults (dry_run/staging + no credentials) for them.
_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


# --- Enums make invalid modes impossible to represent ----------------------
# Using enums (rather than free strings) means an out-of-range value fails
# loudly at settings-load time instead of silently mis-routing at runtime.


class VisionEnv(str, Enum):
    """Runtime mode governing fail-safe behaviour (FR-20)."""

    DRY_RUN = "dry_run"  # no email, no post — safest default
    STAGING = "staging"  # email self, post-then-delete test
    LIVE = "live"  # real publish path


class BrahmastraMode(str, Enum):
    """Brahmastra invocation strategy. This build is CLI-only (no API keys)."""

    CLI = "cli"
    API = "api"  # reserved; not used in this build (kept for forward-compat)


class PublishMode(str, Enum):
    """LinkedIn publish strategy (§15.5)."""

    API = "api"  # official /rest/posts path
    PREFILL = "prefill"  # degraded manual-composer fallback


class SignatureMode(str, Enum):
    """How the Brahmastra signature is applied (§15.6, D9)."""

    OFF = "off"
    CARD_WATERMARK = "card_watermark"
    TEXT_FOOTER = "text_footer"
    BOTH = "both"


class Settings(BaseSettings):
    """All VISION configuration, loaded from environment / ``.env``.

    Each field maps 1:1 onto a BRD Appendix-A variable. Defaults are chosen to
    be *safe in development*: SQLite database, dry-run mode, CLI Brahmastra — so
    an un-configured checkout can run tests without touching any external system
    or leaking to a live profile.
    """

    # pydantic-settings config: read a local ``.env``, ignore unknown keys so a
    # richer deployment ``.env`` never crashes the app, and treat env var names
    # case-insensitively to match shell conventions.
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Core --------------------------------------------------------------
    # Runtime mode + timezone. TZ drives every local schedule calculation so
    # the publish slot / approval cutoff resolve to the owner's wall clock.
    vision_env: VisionEnv = Field(default=VisionEnv.DRY_RUN, alias="VISION_ENV")
    tz: str = Field(default="Asia/Kolkata", alias="TZ")
    # SQLite by default keeps dev/test hermetic; prod overrides with a Postgres URL.
    database_url: str = Field(default="sqlite:///vision.db", alias="DATABASE_URL")
    # Secrets: dev placeholders let tests run, but prod MUST override both. These
    # are never logged (the logging redaction filter masks *_key/*secret fields).
    secret_hmac_key: str = Field(default="dev-insecure-hmac-key", alias="SECRET_HMAC_KEY")
    token_enc_key: str = Field(default="dev-insecure-token-enc-key", alias="TOKEN_ENC_KEY")

    # --- LinkedIn ----------------------------------------------------------
    li_client_id: str = Field(default="", alias="LI_CLIENT_ID")
    li_client_secret: str = Field(default="", alias="LI_CLIENT_SECRET")
    li_redirect_uri: str = Field(
        default="https://localhost/oauth/linkedin/callback", alias="LI_REDIRECT_URI"
    )
    # LinkedIn-Version header value, YYYYMM (§6). Pinned so API drift is explicit.
    li_version: str = Field(default="202506", alias="LI_VERSION")
    publish_mode: PublishMode = Field(default=PublishMode.API, alias="PUBLISH_MODE")
    # Local time-of-day strings; parsed by the scheduler against ``tz``.
    publish_slot_local: str = Field(default="09:00", alias="PUBLISH_SLOT_LOCAL")
    approve_cutoff_local: str = Field(default="20:00", alias="APPROVE_CUTOFF_LOCAL")

    # --- Email -------------------------------------------------------------
    email_provider: str = Field(default="smtp", alias="EMAIL_PROVIDER")
    email_from: str = Field(default="vision@localhost", alias="EMAIL_FROM")
    email_to: str = Field(default="owner@localhost", alias="EMAIL_TO")
    email_api_key: str = Field(default="", alias="EMAIL_API_KEY")

    # --- Brahmastra / models ----------------------------------------------
    # CLI-only invocation: VISION shells out to the council scripts; no API keys.
    brahmastra_mode: BrahmastraMode = Field(
        default=BrahmastraMode.CLI, alias="BRAHMASTRA_MODE"
    )
    # Council dir is configurable; default expands ~ so it points at the user's
    # ~/.claude/council without hard-coding an absolute path.
    brahmastra_council_dir: Path = Field(
        default=Path("~/.claude/council").expanduser(), alias="BRAHMASTRA_COUNCIL_DIR"
    )
    # Lane names routed onto the CLI scripts for the three synthesis passes (§13.1).
    model_generate: str = Field(default="gemini", alias="MODEL_GENERATE")
    model_critique: str = Field(default="codex", alias="MODEL_CRITIQUE")
    model_verify: str = Field(default="claude", alias="MODEL_VERIFY")

    # --- Council engine (BRD §5 evolution / council-content-vision) --------
    # WHY these live in Settings: the council is a config-over-code (§22.6)
    # feature — whether it runs, where its owner topic queue lives, which topics
    # it must never touch, the Claude binary name, and how many recent formats to
    # avoid repeating are ALL owner-editable knobs, never baked into code. Each
    # maps onto a CLI/env var so a deployment tunes the council without a code
    # change.
    #
    # Master on/off switch. Fail-closed default is DISABLED so an un-configured
    # checkout never fires the council path by accident (§22.9).
    council_enabled: bool = Field(default=False, alias="COUNCIL_ENABLED")
    # File of owner-supplied topics, one per line, consumed FIFO before the
    # council proposes its own. Expanduser'd at read time (see topics.py) so a
    # '~/...'  path resolves on every OS. Empty string => no queue file.
    council_topic_queue_path: str = Field(
        default="prep/council_topics.txt", alias="COUNCIL_TOPIC_QUEUE_PATH"
    )
    # Guardrail: topics the council must NEVER touch. Any proposed/queued topic
    # whose text contains one of these (case-insensitive substring) is filtered
    # out. Editable via a comma-separated env value (pydantic parses the list).
    council_exclusions: list[str] = Field(
        default_factory=list, alias="COUNCIL_EXCLUSIONS"
    )
    # The Claude CLI binary used for the compose/deliberate 'claude -p' voice.
    # Configurable because the launcher name/path varies per host (§22.6).
    council_claude_bin: str = Field(default="claude", alias="COUNCIL_CLAUDE_BIN")
    # How many most-recent formats to avoid repeating (the format-variety window).
    council_recent_window: int = Field(default=4, alias="COUNCIL_RECENT_WINDOW")
    # Where recent-format history + queue cursor are persisted. NOT hard-coded to
    # prep/: a configurable state file (expanduser'd) so the council's memory
    # lives wherever the deployment wants it.
    council_state_path: str = Field(
        default="prep/.council_state.json", alias="COUNCIL_STATE_PATH"
    )

    # --- Council image lane (BRD §13.6 wired into the council) -------------
    # WHY these live in Settings (config over code, §22.6): whether the council
    # attaches an image, HOW OFTEN, and WHERE the PNGs land are all owner-editable
    # knobs — never baked into code. The council image lane is a best-effort
    # enhancement that MUST degrade to text-only on any failure (§13.6), so its
    # policy is config, not hard-coded branching.
    #
    # Master on/off for the council image lane specifically (separate from the
    # global IMAGE_ENABLED so the owner can silence council images without
    # touching the news lane). Fail-closed default OFF: an un-configured checkout
    # never attaches a council image by accident (§22.9).
    council_image_enabled: bool = Field(
        default=False, alias="COUNCIL_IMAGE_ENABLED"
    )
    # Rotation heuristic: attach an image on roughly 1-in-N council posts so a
    # council draft is NOT image-heavy (the post is the idea; the image is
    # occasional seasoning). 1 => every eligible post; a larger N spaces them out.
    council_image_every_n: int = Field(
        default=3, alias="COUNCIL_IMAGE_EVERY_N"
    )
    # Directory the rendered/generated council PNGs are written to (the mailer +
    # publisher later read ``draft.image_path`` from here). Expanduser'd at read
    # time. Default under the system temp dir so a bare checkout works; prod points
    # it at a durable volume.
    council_image_dir: str = Field(
        default="", alias="COUNCIL_IMAGE_DIR"
    )
    # Where the council image lane persists its weekly-cap ledger + rotation
    # counter (a small JSON state file, expanduser'd). Kept separate from the
    # format-variety state so the two concerns never collide on disk.
    council_image_state_path: str = Field(
        default="prep/.council_image_state.json", alias="COUNCIL_IMAGE_STATE_PATH"
    )

    # --- Content -----------------------------------------------------------
    recency_hours: int = Field(default=48, alias="RECENCY_HOURS")
    grounding_min_pct: int = Field(default=100, alias="GROUNDING_MIN_PCT")
    dedup_sim_threshold: float = Field(default=0.80, alias="DEDUP_SIM_THRESHOLD")

    # --- Images / visuals (§13.6) -----------------------------------------
    image_enabled: bool = Field(default=True, alias="IMAGE_ENABLED")
    image_model: str = Field(default="gemini-image", alias="IMAGE_MODEL")
    # The CONFIRMED-WORKING AI-image path (verified live 2026-07-08): agy
    # (Antigravity/Gemini) runs as an AGENT under the owner's subscription — NO
    # API key — and writes real PNGs to a path. WHY config not code (§22.6): the
    # binary location varies per host, and the legacy 'gemini' CLI / gemini_image.sh
    # path is DEAD (IneligibleTierError), so the working launcher must be an
    # owner-editable knob, never hard-coded. Default is the verified install path.
    agy_bin: str = Field(
        default="/c/Users/vishn/AppData/Local/agy/bin/agy", alias="AGY_BIN"
    )
    image_max_per_week: int = Field(default=4, alias="IMAGE_MAX_PER_WEEK")
    image_style_guide: str = Field(
        default=(
            "elevated hand-drawn anime and manga art, editorial and tasteful, "
            "emotive, refined linework, no photorealism"
        ),
        alias="IMAGE_STYLE_GUIDE",
    )
    card_brand_palette: str = Field(
        default="navy=#0B1F3A;gold=#C9A24B", alias="CARD_BRAND_PALETTE"
    )

    # --- Data lifecycle: retention + backup + prune -----------------------
    # A weekly job archives rows/images older than the window, backs the archive
    # up to Google Drive via rclone, VERIFIES the upload, and only THEN prunes
    # locally + VACUUMs. Fail-closed: nothing is pruned without a verified backup.
    retention_enabled: bool = Field(default=True, alias="RETENTION_ENABLED")
    # ge=1 guards a destructive job against a fat-fingered 0/negative window that
    # would archive+prune everything at once (Codex review).
    retention_days: int = Field(default=30, ge=1, alias="RETENTION_DAYS")
    retention_archive_dir: str = Field(default="archive", alias="RETENTION_ARCHIVE_DIR")
    # rclone drives the off-box backup. An empty remote name = Drive upload is not
    # configured yet: the job archives locally and SKIPS the prune (never deletes
    # data it could not back up).
    rclone_bin: str = Field(default="rclone", alias="RCLONE_BIN")
    rclone_remote: str = Field(default="", alias="RCLONE_REMOTE")
    rclone_drive_path: str = Field(default="VISION/backups", alias="RCLONE_DRIVE_PATH")

    # --- Video lane (Phase 5 — anime Insight Reels) -----------------------
    # Opt-in, never forced daily. No API key: agy stills + edge-tts voice +
    # imageio-ffmpeg's bundled binary. Veo B-roll is a later opt-in phase.
    video_enabled: bool = Field(default=False, alias="VIDEO_ENABLED")
    video_voice: str = Field(default="en-US-AndrewNeural", alias="VIDEO_VOICE")
    video_width: int = Field(default=1080, ge=16, alias="VIDEO_WIDTH")
    video_height: int = Field(default=1920, ge=16, alias="VIDEO_HEIGHT")
    video_fps: int = Field(default=30, ge=1, le=60, alias="VIDEO_FPS")
    video_work_dir: str = Field(default="prep/reels", alias="VIDEO_WORK_DIR")
    video_music_dir: str = Field(default="assets/music", alias="VIDEO_MUSIC_DIR")

    # --- Author / signature (§15.6, D9) -----------------------------------
    post_signature_mode: SignatureMode = Field(
        default=SignatureMode.CARD_WATERMARK, alias="POST_SIGNATURE_MODE"
    )
    post_signature_text: str = Field(
        default="— curated via Brahmastra, my multi-AI system",
        alias="POST_SIGNATURE_TEXT",
    )
    brahmastra_logo_path: Path = Field(
        default=Path("/opt/vision/assets/brahmastra_logo.svg"),
        alias="BRAHMASTRA_LOGO_PATH",
    )

    # --- Derived convenience ----------------------------------------------
    @property
    def is_sqlite(self) -> bool:
        """True when the configured DB is SQLite.

        Used by the session/engine layer to apply SQLite-only connect args
        (``check_same_thread``) without leaking DB-specific logic elsewhere.
        """
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    """Return a process-wide singleton ``Settings`` instance.

    Caching avoids re-parsing the environment on every access and guarantees a
    single, consistent configuration object across the app. Tests that need a
    fresh instance can call ``get_settings.cache_clear()``.
    """
    return Settings()

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
        env_file=".env",
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

    # --- Content -----------------------------------------------------------
    recency_hours: int = Field(default=48, alias="RECENCY_HOURS")
    grounding_min_pct: int = Field(default=100, alias="GROUNDING_MIN_PCT")
    dedup_sim_threshold: float = Field(default=0.80, alias="DEDUP_SIM_THRESHOLD")

    # --- Images / visuals (§13.6) -----------------------------------------
    image_enabled: bool = Field(default=True, alias="IMAGE_ENABLED")
    image_model: str = Field(default="gemini-image", alias="IMAGE_MODEL")
    image_max_per_week: int = Field(default=4, alias="IMAGE_MAX_PER_WEEK")
    image_style_guide: str = Field(
        default="minimal, professional, muted palette, no text, no logos",
        alias="IMAGE_STYLE_GUIDE",
    )
    card_brand_palette: str = Field(
        default="navy=#0B1F3A;gold=#C9A24B", alias="CARD_BRAND_PALETTE"
    )

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

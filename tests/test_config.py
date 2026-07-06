"""Config smoke tests — assert the Settings defaults load and are typed.

Follows AAA: arrange (clear cache), act (load settings), assert (defaults).
"""

from __future__ import annotations

from vision.config import (
    BrahmastraMode,
    PublishMode,
    SignatureMode,
    VisionEnv,
    get_settings,
)


def test_settings_defaults_load_with_safe_dev_values() -> None:
    # Arrange: clear the lru_cache so we read a fresh instance (not a cached one
    # left by another test), then act.
    get_settings.cache_clear()
    settings = get_settings()

    # Assert: the dev-safe defaults from BRD Appendix A are present and typed.
    assert settings.vision_env is VisionEnv.DRY_RUN
    assert settings.tz == "Asia/Kolkata"
    assert settings.database_url.startswith("sqlite")
    assert settings.brahmastra_mode is BrahmastraMode.CLI
    assert settings.publish_mode is PublishMode.API
    assert settings.post_signature_mode is SignatureMode.CARD_WATERMARK


def test_content_thresholds_have_expected_defaults() -> None:
    # Arrange / Act.
    get_settings.cache_clear()
    settings = get_settings()

    # Assert: the content gates match Appendix A defaults.
    assert settings.recency_hours == 48
    assert settings.grounding_min_pct == 100
    assert settings.dedup_sim_threshold == 0.80


def test_is_sqlite_helper_reflects_default_url() -> None:
    # Arrange / Act.
    get_settings.cache_clear()
    settings = get_settings()

    # Assert: the derived helper agrees with the default SQLite URL.
    assert settings.is_sqlite is True

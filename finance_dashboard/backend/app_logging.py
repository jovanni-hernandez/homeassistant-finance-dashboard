"""Persisted application log verbosity controls."""
from database import get_setting, set_setting

APP_LOG_LEVEL_KEY = "app_log_level"
DEFAULT_APP_LOG_LEVEL = "INFO"
ALLOWED_APP_LOG_LEVELS = {"INFO", "DEBUG"}


def get_app_log_settings() -> dict:
    level = (get_setting(APP_LOG_LEVEL_KEY) or DEFAULT_APP_LOG_LEVEL).strip().upper()
    if level not in ALLOWED_APP_LOG_LEVELS:
        level = DEFAULT_APP_LOG_LEVEL
    return {
        "level": level,
        "allowed_levels": sorted(ALLOWED_APP_LOG_LEVELS),
        "full_ai_payload_logging": level == "DEBUG",
    }


def apply_app_log_level(level: str | None = None) -> str:
    selected = (level or get_app_log_settings()["level"]).strip().upper()
    if selected not in ALLOWED_APP_LOG_LEVELS:
        raise ValueError("Log level must be INFO or DEBUG.")
    set_setting(APP_LOG_LEVEL_KEY, selected)
    import logging

    logging.getLogger("finance_dashboard").setLevel(getattr(logging, selected))
    return selected

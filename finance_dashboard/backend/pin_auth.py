import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from database import get_setting, set_setting

PIN_ENABLED_KEY = "pin_enabled"
PIN_SALT_KEY = "pin_salt"
PIN_HASH_KEY = "pin_hash"
PIN_IDLE_TIMEOUT_KEY = "pin_idle_timeout_minutes"
PIN_SESSION_TIMEOUT_KEY = "pin_session_timeout_minutes"
PIN_SESSION_HASH_KEY = "pin_session_token_hash"
PIN_SESSION_ISSUED_KEY = "pin_session_issued_at"
PIN_SESSION_LAST_USED_KEY = "pin_session_last_used_at"
PIN_SESSION_EXPIRES_KEY = "pin_session_expires_at"

DEFAULT_IDLE_TIMEOUT_MINUTES = 10
DEFAULT_SESSION_TIMEOUT_MINUTES = 480
MIN_PIN_LENGTH = 4
PBKDF2_ITERATIONS = 200_000


def _now():
    return datetime.now(timezone.utc)


def _parse_bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _parse_int(value, default):
    try:
        return int(str(value).strip())
    except Exception:
        return default


def _clamp_int(value, default, minimum, maximum):
    parsed = _parse_int(value, default)
    return max(minimum, min(maximum, parsed))


def _parse_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def get_pin_settings():
    enabled = _parse_bool(get_setting(PIN_ENABLED_KEY), False)
    configured = bool(get_setting(PIN_HASH_KEY) and get_setting(PIN_SALT_KEY))
    idle_timeout_minutes = _clamp_int(get_setting(PIN_IDLE_TIMEOUT_KEY), DEFAULT_IDLE_TIMEOUT_MINUTES, 1, 24 * 60)
    session_timeout_minutes = _clamp_int(get_setting(PIN_SESSION_TIMEOUT_KEY), DEFAULT_SESSION_TIMEOUT_MINUTES, 5, 14 * 24 * 60)
    return {
        "enabled": enabled,
        "configured": configured,
        "pin_required": enabled and configured,
        "idle_timeout_minutes": idle_timeout_minutes,
        "session_timeout_minutes": session_timeout_minutes,
    }


def pin_is_required():
    settings = get_pin_settings()
    return settings["pin_required"]


def _hash_pin(pin: str, salt_b64: str | None = None):
    salt = base64.urlsafe_b64decode(salt_b64.encode("utf-8")) if salt_b64 else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return base64.urlsafe_b64encode(digest).decode("utf-8"), base64.urlsafe_b64encode(salt).decode("utf-8")


def set_pin(pin: str):
    if not pin or len(pin.strip()) < MIN_PIN_LENGTH:
        raise ValueError(f"PIN must be at least {MIN_PIN_LENGTH} characters.")

    hashed, salt = _hash_pin(pin.strip())
    set_setting(PIN_HASH_KEY, hashed)
    set_setting(PIN_SALT_KEY, salt)


def verify_pin(pin: str):
    stored_hash = get_setting(PIN_HASH_KEY)
    stored_salt = get_setting(PIN_SALT_KEY)
    if not stored_hash or not stored_salt or not pin:
        return False

    candidate, _ = _hash_pin(pin.strip(), stored_salt)
    return hmac.compare_digest(candidate, stored_hash)


def clear_session():
    set_setting(PIN_SESSION_HASH_KEY, "")
    set_setting(PIN_SESSION_ISSUED_KEY, "")
    set_setting(PIN_SESSION_LAST_USED_KEY, "")
    set_setting(PIN_SESSION_EXPIRES_KEY, "")


def issue_session_token():
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = _now()
    settings = get_pin_settings()
    expires_at = now + timedelta(minutes=settings["session_timeout_minutes"])

    set_setting(PIN_SESSION_HASH_KEY, token_hash)
    set_setting(PIN_SESSION_ISSUED_KEY, now.isoformat())
    set_setting(PIN_SESSION_LAST_USED_KEY, now.isoformat())
    set_setting(PIN_SESSION_EXPIRES_KEY, expires_at.isoformat())

    return token, expires_at.isoformat()


def validate_session_token(token: str):
    settings = get_pin_settings()
    if not settings["pin_required"]:
        return True

    if not token:
        return False

    stored_hash = get_setting(PIN_SESSION_HASH_KEY)
    if not stored_hash:
        return False

    candidate_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(candidate_hash, stored_hash):
        return False

    now = _now()
    expires_at = _parse_dt(get_setting(PIN_SESSION_EXPIRES_KEY))
    if expires_at and now > expires_at:
        clear_session()
        return False

    last_used_at = _parse_dt(get_setting(PIN_SESSION_LAST_USED_KEY))
    if last_used_at:
        idle_limit = timedelta(minutes=settings["idle_timeout_minutes"])
        if now - last_used_at > idle_limit:
            clear_session()
            return False

    set_setting(PIN_SESSION_LAST_USED_KEY, now.isoformat())
    return True


def unlock_pin(pin: str):
    if not verify_pin(pin):
        return None

    token, expires_at = issue_session_token()
    settings = get_pin_settings()
    return {
        "access_token": token,
        "expires_at": expires_at,
        "idle_timeout_minutes": settings["idle_timeout_minutes"],
        "session_timeout_minutes": settings["session_timeout_minutes"],
    }


def update_pin_settings(enabled: bool, idle_timeout_minutes: int, session_timeout_minutes: int, new_pin: str | None = None):
    idle_timeout_minutes = max(1, min(24 * 60, int(idle_timeout_minutes)))
    session_timeout_minutes = max(5, min(14 * 24 * 60, int(session_timeout_minutes)))

    if new_pin is not None and new_pin.strip():
        set_pin(new_pin)
        enabled = True
    elif enabled and not get_setting(PIN_HASH_KEY):
        raise ValueError("Set a PIN before enabling PIN protection.")

    set_setting(PIN_ENABLED_KEY, "true" if enabled else "false")
    set_setting(PIN_IDLE_TIMEOUT_KEY, str(idle_timeout_minutes))
    set_setting(PIN_SESSION_TIMEOUT_KEY, str(session_timeout_minutes))

    if new_pin is not None and new_pin.strip():
        clear_session()

    if not enabled:
        clear_session()

    return get_pin_settings()


def get_pin_status(token: str | None = None):
    settings = get_pin_settings()
    session_active = True
    if settings["pin_required"]:
        session_active = validate_session_token(token or "")

    return {
        **settings,
        "session_active": session_active,
        "locked": settings["pin_required"] and not session_active,
    }

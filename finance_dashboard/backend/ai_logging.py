"""Full AI exchange logging with API-key redaction."""
from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger("finance_dashboard")

_KNOWN_KEY_PATTERNS = (
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
)
_NAMED_KEY_PATTERN = re.compile(
    r"(?i)([\"']?(?:api[_-]?key|authorization)[\"']?\s*[:=]\s*[\"']?)([^\"',}\s]+)"
)


def redact_api_keys(value: str, api_key: str | None = None) -> str:
    redacted = value
    if api_key:
        redacted = redacted.replace(api_key, "[REDACTED_API_KEY]")
    redacted = _NAMED_KEY_PATTERN.sub(r"\1[REDACTED_API_KEY]", redacted)
    for pattern in _KNOWN_KEY_PATTERNS:
        redacted = pattern.sub("[REDACTED_API_KEY]", redacted)
    return redacted


def log_ai_exchange(
    *,
    event: str,
    provider: str,
    model: str,
    request,
    api_key: str | None = None,
    response=None,
    error=None,
    status: int | str | None = None,
    attempt: int | None = None,
):
    """Log complete request/response payloads, excluding keys and auth tokens."""
    entry = {
        "event": event,
        "provider": provider,
        "model": model,
        "request": request,
        "response": response,
        "error": str(error) if error is not None else None,
        "status": status,
        "attempt": attempt,
    }
    serialized = json.dumps(entry, ensure_ascii=False, default=str, separators=(",", ":"))
    logger.debug("AI_FULL_EXCHANGE %s", redact_api_keys(serialized, api_key))

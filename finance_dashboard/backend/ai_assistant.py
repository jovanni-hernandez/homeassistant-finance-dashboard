import json
import logging
import re

from category_analysis import generate_category_analysis
from database import get_setting, set_setting

logger = logging.getLogger("finance_dashboard")

AI_ENABLED_KEY = "ai_enabled"
AI_PROVIDER_KEY = "ai_provider"
AI_MODEL_KEY = "ai_model"
AI_OPENAI_KEY = "ai_openai_api_key"
AI_CLAUDE_KEY = "ai_claude_api_key"
AI_GEMINI_KEY = "ai_gemini_api_key"

DEFAULT_PROVIDER = "openai"
DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "claude": "claude-3-5-sonnet-latest",
    "gemini": "gemini/gemini-2.0-flash",
}


def _parse_bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def get_ai_settings():
    provider = (get_setting(AI_PROVIDER_KEY) or DEFAULT_PROVIDER).strip().lower()
    if provider not in DEFAULT_MODELS:
        provider = DEFAULT_PROVIDER

    return {
        "enabled": _parse_bool(get_setting(AI_ENABLED_KEY), False),
        "provider": provider,
        "model": get_setting(AI_MODEL_KEY) or DEFAULT_MODELS[provider],
        "openai_key_set": bool(get_setting(AI_OPENAI_KEY)),
        "claude_key_set": bool(get_setting(AI_CLAUDE_KEY)),
        "gemini_key_set": bool(get_setting(AI_GEMINI_KEY)),
    }


def update_ai_settings(enabled: bool, provider: str, model: str, openai_api_key: str | None = None, claude_api_key: str | None = None, gemini_api_key: str | None = None):
    provider = (provider or DEFAULT_PROVIDER).strip().lower()
    if provider not in DEFAULT_MODELS:
        raise ValueError(f"Unsupported provider: {provider}")

    model = (model or DEFAULT_MODELS[provider]).strip()
    if not model:
        model = DEFAULT_MODELS[provider]

    set_setting(AI_ENABLED_KEY, "true" if enabled else "false")
    set_setting(AI_PROVIDER_KEY, provider)
    set_setting(AI_MODEL_KEY, model)

    if openai_api_key is not None and openai_api_key.strip():
        set_setting(AI_OPENAI_KEY, openai_api_key.strip())
    if claude_api_key is not None and claude_api_key.strip():
        set_setting(AI_CLAUDE_KEY, claude_api_key.strip())
    if gemini_api_key is not None and gemini_api_key.strip():
        set_setting(AI_GEMINI_KEY, gemini_api_key.strip())

    return get_ai_settings()


def _provider_key(provider: str):
    provider = (provider or DEFAULT_PROVIDER).strip().lower()
    return {
        "openai": get_setting(AI_OPENAI_KEY),
        "claude": get_setting(AI_CLAUDE_KEY),
        "gemini": get_setting(AI_GEMINI_KEY),
    }.get(provider)


def _extract_json_array(text: str):
    if not text:
        return None

    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, list):
            return parsed
    except Exception:
        pass

    match = re.search(r"\[[\s\S]*\]", cleaned)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass

    return None


def _normalize_rules(candidate):
    if not isinstance(candidate, list):
        return []

    rules = []
    for item in candidate:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category", "")).strip()
        patterns = item.get("patterns", [])
        if not category or not isinstance(patterns, list):
            continue
        normalized_patterns = []
        for pattern in patterns:
            pattern = str(pattern).strip()
            if pattern and pattern not in normalized_patterns:
                normalized_patterns.append(pattern)
        if normalized_patterns:
            rules.append({"category": category, "patterns": normalized_patterns})
    return rules


def build_ai_category_defaults(top_n: int = 25, min_count: int = 2):
    analysis = generate_category_analysis(top_n=top_n, min_count=min_count)
    settings = get_ai_settings()
    provider = settings["provider"]
    model = settings["model"]
    api_key = _provider_key(provider)

    if not settings["enabled"]:
        raise ValueError("AI assistance is disabled in settings.")
    if not api_key:
        raise ValueError(f"No API key saved for provider '{provider}'.")

    try:
        from litellm import completion
    except Exception as exc:
        raise RuntimeError(f"LiteLLM is not available: {exc}")

    system_prompt = (
        "You are a finance categorization assistant.\n"
        "Return ONLY valid JSON as an array of objects.\n"
        "Each object must look like {\"category\": \"Name\", \"patterns\": [\"pattern1\", \"pattern2\"]}.\n"
        "Use concise merchant/payee patterns that are likely to match future transactions.\n"
        "Prefer exact merchant names, common abbreviations, and stable keyword fragments.\n"
        "Do not add commentary, markdown, or code fences."
    )

    user_prompt = json.dumps(
        {
            "current_category_mix": analysis.get("current_category_mix", []),
            "deterministic_proposals": analysis.get("proposals", []),
            "uncategorized_candidates": analysis.get("uncategorized_candidates", []),
            "existing_rules_json": analysis.get("rules_json", "[]"),
            "instructions": {
                "target_output": "array of category rule objects",
                "minimum_categories": 4,
                "maximum_patterns_per_category": 12,
                "avoid_duplicates": True,
            },
        },
        indent=2,
    )

    response = completion(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        api_key=api_key,
        temperature=0,
    )

    content = ""
    try:
        content = response.choices[0].message.content or ""
    except Exception:
        content = str(response)

    parsed = _extract_json_array(content)
    rules = _normalize_rules(parsed)
    if not rules:
        raise ValueError("AI did not return usable category rules.")

    return {
        **analysis,
        "provider": provider,
        "model": model,
        "rules_json": json.dumps(rules, indent=2),
    }

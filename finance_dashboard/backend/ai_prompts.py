"""Default and user-overridable system prompts used by Finance Dashboard AI."""
from database import get_setting, set_setting

MAX_SYSTEM_PROMPT_LENGTH = 12000

DEFAULT_SYSTEM_PROMPTS = {
    "goal_planner": """You help the user think through financial goals using a minimized financial snapshot.
The snapshot contains aggregate balances grouped by currency and account type, monthly income and category spending totals, and unnamed goal summaries. It contains no account names, account IDs, merchant names, or transaction descriptions. Never ask for account names, account numbers, or other identifying details.

Use only the information provided. Keep currencies separate; never convert currencies or invent balances. Treat projections as estimates, not guarantees or professional financial advice. Do not assume an APR, return, inflation rate, or contribution amount unless the user or snapshot provides it. Ask a concise follow-up if important inputs are missing.

When you have enough information to suggest one concrete goal, include exactly one final structured block in this format, with no markdown fence:
<goal_draft>{\"goal_type\":\"savings|debt_payoff|retirement\",\"name\":\"short non-identifying label\",\"target_amount\":\"number\",\"target_date\":\"YYYY-MM-DD\",\"apr_percent\":\"number\",\"planned_monthly\":\"number\",\"retirement_timing_mode\":\"year|age\",\"retirement_year\":0,\"birth_year\":0,\"target_retirement_age\":0,\"expected_return_percent\":\"number\",\"inflation_percent\":\"number\"}</goal_draft>
Include only relevant fields. Never include account IDs or account names in a draft. The app will require the user to choose sources and review/save the draft.""",
    "category_rules": """You are a finance categorization assistant.
Return ONLY valid JSON as an array of objects.
Each object must look like {\"category\": \"Name\", \"patterns\": [\"pattern1\", \"pattern2\"]}.
Use concise merchant/payee patterns that are likely to match future transactions.
Prefer exact merchant names, common abbreviations, and stable keyword fragments.
Do not add commentary, markdown, or code fences.""",
}

PROMPT_SETTING_KEYS = {
    "goal_planner": "ai_prompt_goal_planner_override",
    "category_rules": "ai_prompt_category_rules_override",
}


class AIPromptValidationError(ValueError):
    pass


def get_ai_system_prompt(prompt_id: str) -> str:
    if prompt_id not in DEFAULT_SYSTEM_PROMPTS:
        raise AIPromptValidationError("Unknown AI system prompt.")
    override = get_setting(PROMPT_SETTING_KEYS[prompt_id])
    return override if override else DEFAULT_SYSTEM_PROMPTS[prompt_id]


def get_ai_prompt_settings() -> dict:
    prompts = {}
    for prompt_id, default_prompt in DEFAULT_SYSTEM_PROMPTS.items():
        override = get_setting(PROMPT_SETTING_KEYS[prompt_id])
        prompts[prompt_id] = {
            "prompt": override if override else default_prompt,
            "default_prompt": default_prompt,
            "is_custom": bool(override),
        }
    return {"prompts": prompts, "max_length": MAX_SYSTEM_PROMPT_LENGTH}


def save_ai_system_prompt(prompt_id: str, prompt: str | None = None, *, reset_to_default: bool = False) -> dict:
    if prompt_id not in DEFAULT_SYSTEM_PROMPTS:
        raise AIPromptValidationError("Unknown AI system prompt.")

    setting_key = PROMPT_SETTING_KEYS[prompt_id]
    if reset_to_default:
        # Settings are encrypted by the Setting model; an empty override means
        # the shipped default is active while keeping the reset idempotent.
        set_setting(setting_key, "")
    else:
        if not isinstance(prompt, str) or not prompt.strip():
            raise AIPromptValidationError("A system prompt cannot be blank. Use Reset to default instead.")
        if len(prompt) > MAX_SYSTEM_PROMPT_LENGTH:
            raise AIPromptValidationError(f"System prompts must be {MAX_SYSTEM_PROMPT_LENGTH} characters or fewer.")
        set_setting(setting_key, prompt)

    return get_ai_prompt_settings()["prompts"][prompt_id]

#!/usr/bin/env python3
"""Tests provider-specific AI model discovery without network calls."""
from __future__ import annotations

import atexit
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
TEST_ROOT = Path(os.environ.get("FINANCE_TEST_ARTIFACT_DIR", "/data/v2/cache/opencode"))
TEST_ROOT.mkdir(parents=True, exist_ok=True)
TEST_DATA = tempfile.TemporaryDirectory(prefix="finance-ai-models-", dir=TEST_ROOT)
atexit.register(TEST_DATA.cleanup)
os.environ["FINANCE_DB_PATH"] = str(Path(TEST_DATA.name) / "test.sqlite")
os.environ["FINANCE_KEY_PATH"] = str(Path(TEST_DATA.name) / "test.key")
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

import ai_assistant  # noqa: E402
import main  # noqa: E402
from database import get_setting, init_db, set_setting  # noqa: E402
from models import SessionLocal, Setting  # noqa: E402

init_db()


class AIModelDiscoveryTests(unittest.TestCase):
    def setUp(self):
        db = SessionLocal()
        try:
            db.query(Setting).delete()
            db.commit()
        finally:
            db.close()

    @staticmethod
    def response(payload, status_code=200):
        response = requests.Response()
        response.status_code = status_code
        response._content = json.dumps(payload).encode()
        response.headers["Content-Type"] = "application/json"
        return response

    def test_openai_lists_chat_models_and_filters_non_chat_models(self):
        response = self.response({"data": [
            {"id": "gpt-4o-mini"},
            {"id": "o3-mini"},
            {"id": "text-embedding-3-small"},
            {"id": "whisper-1"},
        ]})
        with patch.object(ai_assistant.requests, "get", return_value=response) as get:
            models = ai_assistant.list_provider_models("openai", "test-openai-key")

        self.assertEqual(models, ["gpt-4o-mini", "o3-mini"])
        self.assertEqual(get.call_args.args[0], "https://api.openai.com/v1/models")
        self.assertEqual(get.call_args.kwargs["headers"], {"Authorization": "Bearer test-openai-key"})
        self.assertEqual(get.call_args.kwargs["timeout"], 15)

    def test_anthropic_uses_required_headers_and_returns_model_ids(self):
        response = self.response({"data": [{"id": "claude-sonnet-latest"}, {"id": "claude-haiku-latest"}]})
        with patch.object(ai_assistant.requests, "get", return_value=response) as get:
            models = ai_assistant.list_provider_models("claude", "test-claude-key")

        self.assertEqual(models, ["claude-haiku-latest", "claude-sonnet-latest"])
        self.assertEqual(get.call_args.kwargs["headers"], {
            "x-api-key": "test-claude-key",
            "anthropic-version": "2023-06-01",
        })

    def test_gemini_returns_only_generate_content_models_with_litellm_prefix(self):
        response = self.response({"models": [
            {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
        ]})
        with patch.object(ai_assistant.requests, "get", return_value=response) as get:
            models = ai_assistant.list_provider_models("gemini", "test-gemini-key")

        self.assertEqual(models, ["gemini/gemini-2.5-flash"])
        self.assertEqual(get.call_args.kwargs["headers"], {"x-goog-api-key": "test-gemini-key"})
        self.assertNotIn("test-gemini-key", get.call_args.args[0])

    def test_discovery_uses_a_saved_key_when_no_new_key_is_sent(self):
        set_setting(ai_assistant.AI_OPENAI_KEY, "saved-openai-key")
        response = self.response({"data": [{"id": "gpt-4o-mini"}]})
        with patch.object(ai_assistant.requests, "get", return_value=response) as get:
            models = ai_assistant.list_provider_models("openai")

        self.assertEqual(models, ["gpt-4o-mini"])
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer saved-openai-key")

    def test_discovery_endpoint_does_not_persist_the_submitted_key(self):
        response = self.response({"data": [{"id": "gpt-4o-mini"}]})
        with patch.object(ai_assistant.requests, "get", return_value=response):
            result = main.api_list_ai_models(main.AiModelsRequest(provider="openai", api_key="temporary-key"))

        self.assertEqual(result["models"], ["gpt-4o-mini"])
        self.assertIsNone(get_setting(ai_assistant.AI_OPENAI_KEY))

    def test_invalid_key_error_does_not_include_the_key(self):
        response = requests.Response()
        response.status_code = 401
        error = requests.HTTPError("provider rejected request", response=response)
        with patch.object(ai_assistant.requests, "get") as get:
            get.return_value.raise_for_status.side_effect = error
            with self.assertRaises(ai_assistant.ProviderModelDiscoveryError) as raised:
                ai_assistant.list_provider_models("openai", "do-not-leak-this-key")

        self.assertNotIn("do-not-leak-this-key", str(raised.exception))
        self.assertIn("rejected", str(raised.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)

import os
import re
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from oa_agent.config import LoginConfig, ModelConfig, load_settings


class ConfigTests(unittest.TestCase):
    def test_explicit_env_file_and_external_environment_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            env.write_text("OPENAI_API_KEY=dummy-key\nOPENAI_MODEL=file-model\nOA_IDLE_SECONDS=60\nOA_USERNAME=dummy-user\nOA_PASSWORD='dummy-password'\n")
            with patch.dict(os.environ, {"OPENAI_MODEL": "environment-model"}, clear=True):
                settings = load_settings(env)
                self.assertTrue(settings.model.configured)
                self.assertEqual(settings.model.model, "environment-model")
                self.assertEqual(settings.login.idle_seconds, 60)
                self.assertNotIn("dummy-key", repr(settings))
                self.assertTrue(settings.login.auto_login_enabled)
                self.assertNotIn("dummy-password", repr(settings))

    def test_invalid_login_configuration_is_rejected(self):
        for kwargs in ({"idle_seconds": 0}, {"user_info_url": "https://untrusted.example/me"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                LoginConfig(**kwargs)
        with self.assertRaises(re.error):
            LoginConfig(success_url_regex="[")

    def test_missing_model_config_is_not_ready(self):
        self.assertFalse(ModelConfig(api_key="dummy-key").configured)

    def test_reasoning_summary_environment_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            env.write_text("OPENAI_REASONING_SUMMARY=true\n")
            with patch.dict(os.environ, {}, clear=True):
                self.assertTrue(load_settings(env).model.reasoning_summary)
            with patch.dict(os.environ, {"OPENAI_REASONING_SUMMARY": "false"}, clear=True):
                self.assertFalse(load_settings(env).model.reasoning_summary)

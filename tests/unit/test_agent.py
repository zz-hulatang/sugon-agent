import unittest
from unittest.mock import Mock, patch

from oa_agent.agents.oa_agent import build_agent
from oa_agent.agents.prompts import SYSTEM_PROMPT
from oa_agent.config import ModelConfig


class AgentTests(unittest.TestCase):
    def test_model_configuration_and_tool_registration(self):
        tool, employee_tool = Mock(), Mock()
        config = ModelConfig(api_key="dummy-key", model="test-model", base_url="https://model.example/v1")
        with patch("oa_agent.agents.oa_agent.ChatOpenAI") as model, patch("oa_agent.agents.oa_agent.create_agent") as create:
            self.assertIs(build_agent([tool, employee_tool], config), create.return_value)
            self.assertEqual(model.call_args.kwargs["model"], "test-model")
            self.assertEqual(model.call_args.kwargs["base_url"], config.base_url)
            create.assert_called_once_with(model=model.return_value, tools=[tool, employee_tool], system_prompt=SYSTEM_PROMPT)

    def test_missing_credentials_rejected_before_model_creation(self):
        with patch("oa_agent.agents.oa_agent.ChatOpenAI") as model, self.assertRaises(ValueError):
            build_agent([], ModelConfig())
        model.assert_not_called()

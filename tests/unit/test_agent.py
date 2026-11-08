import unittest
from datetime import date, datetime, timezone
from unittest.mock import Mock, patch

from oa_agent.agents.oa_agent import build_agent
from oa_agent.agents.prompts import build_system_prompt
from oa_agent.config import ModelConfig


class AgentTests(unittest.TestCase):
    def test_model_configuration_and_tool_registration(self):
        tool, employee_tool = Mock(), Mock()
        config = ModelConfig(api_key="dummy-key", model="test-model", base_url="https://model.example/v1")
        with patch("oa_agent.agents.oa_agent.ChatOpenAI") as model, patch("oa_agent.agents.oa_agent.create_agent") as create:
            self.assertIs(build_agent([tool, employee_tool], config), create.return_value)
            self.assertEqual(model.call_args.kwargs["model"], "test-model")
            self.assertEqual(model.call_args.kwargs["base_url"], config.base_url)
            create.assert_called_once_with(model=model.return_value, tools=[tool, employee_tool],
                                           system_prompt=build_system_prompt())

    def test_missing_credentials_rejected_before_model_creation(self):
        with patch("oa_agent.agents.oa_agent.ChatOpenAI") as model, self.assertRaises(ValueError):
            build_agent([], ModelConfig())
        model.assert_not_called()


class PromptDateTests(unittest.TestCase):
    def test_previous_and_next_month_cross_year_boundaries(self):
        january = build_system_prompt(date(2026, 1, 1))
        self.assertIn("上月：2025年12月（year=2025, month=12）", january)
        december = build_system_prompt(date(2026, 12, 31))
        self.assertIn("下月：2027年1月（year=2027, month=1）", december)
        leap_day = build_system_prompt(date(2028, 2, 29))
        self.assertIn("下月：2028年3月（year=2028, month=3）", leap_day)

    def test_date_uses_shanghai_when_execution_host_is_still_previous_year(self):
        utc_now = datetime(2025, 12, 31, 16, 30, tzinfo=timezone.utc)
        with patch("oa_agent.agents.prompts.datetime") as clock:
            clock.now.side_effect = lambda tz: utc_now.astimezone(tz)
            prompt = build_system_prompt()
        self.assertIn("当前日期（Asia/Shanghai）：2026-01-01", prompt)
        self.assertIn("本月：2026年1月（year=2026, month=1）", prompt)

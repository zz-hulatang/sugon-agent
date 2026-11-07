"""验证新旧入口、界面会话复用和工具边界；未安装界面依赖时跳过。"""
import importlib.util
from pathlib import Path
import unittest


@unittest.skipUnless(importlib.util.find_spec("streamlit") and importlib.util.find_spec("langchain"), "未安装应用依赖")
class AppTests(unittest.TestCase):
    def test_legacy_main_entry_point(self):
        from streamlit.testing.v1 import AppTest

        app = AppTest.from_file(str(Path(__file__).resolve().parents[2] / "main.py"))
        app.run(timeout=30)
        self.assertEqual(len(app.exception), 0)
        self.addCleanup(app.session_state["oa_session"].close)
        self.assertEqual(app.title[0].value, "OA 智能助手")

    def test_sidebar_rerun_keeps_session_and_handles_missing_login(self):
        from streamlit.testing.v1 import AppTest

        app = AppTest.from_file(str(Path(__file__).resolve().parents[2] / "apps" / "streamlit_app.py"))
        app.run(timeout=30)
        self.assertEqual(len(app.exception), 0)
        session = app.session_state["oa_session"]
        self.addCleanup(session.close)
        self.assertEqual([button.label for button in app.sidebar.button], ["打开 OA 登录", "关闭会话并清除凭据"])
        app.run(timeout=30)
        self.assertEqual(len(app.exception), 0)
        self.assertIs(app.session_state["oa_session"], session)
        self.assertEqual(session.call("status")["state"], "closed")
        app.sidebar.button[1].click().run(timeout=30)
        self.assertEqual(len(app.exception), 0)

    def test_tool_cannot_collect_or_return_credentials(self):
        from pydantic import ValidationError
        from oa_agent.config import LoginConfig
        from oa_agent.schemas.auth import Credentials
        from oa_agent.sessions.session import LoginSession
        from oa_agent.tools.login import make_login_tool

        session = LoginSession(LoginConfig())
        self.addCleanup(session.close)
        session.store.credentials = Credentials(tokens={"DP_Token": "secret-value"})
        tool = make_login_tool(session)
        result = tool.invoke({"action": "status"})
        self.assertNotIn("secret-value", str(result))
        with self.assertRaises(ValidationError):
            tool.invoke({"action": "collect"})

    def test_employee_tool_has_no_account_or_credential_parameters(self):
        from unittest.mock import Mock
        from oa_agent.tools.employee import make_employee_tool

        session = Mock()
        session.call.return_value = {"status": "auth_required", "status_code": 401, "message": "请重新登录"}
        tool = make_employee_tool(session)
        self.assertEqual(tool.args_schema.model_json_schema()["properties"], {})
        self.assertEqual(tool.invoke({})["status_code"], 401)
        session.call.assert_called_once_with("user_info")

    def test_background_task_survives_app_rerun_and_delivers_once(self):
        import threading
        import time
        from types import SimpleNamespace
        from unittest.mock import patch, Mock
        from streamlit.testing.v1 import AppTest
        from oa_agent.config import AppSettings, LoginConfig, ModelConfig

        verified = threading.Event()
        config = AppSettings(LoginConfig(), ModelConfig(api_key="dummy", model="test"))
        agent = Mock()
        agent.invoke.side_effect = lambda *args, **kwargs: (verified.wait(2) or True) and {"messages": [SimpleNamespace(text="原任务完成")]}
        root = Path(__file__).resolve().parents[2]
        with patch("oa_agent.config.load_settings", return_value=config), patch("oa_agent.agents.oa_agent.build_agent", return_value=agent):
            app = AppTest.from_file(str(root / "apps" / "streamlit_app.py")).run(timeout=30)
            self.addCleanup(app.session_state["oa_session"].close)
            app.chat_input[0].set_value("查询我的部门").run(timeout=30)
            runner = app.session_state["chat_tasks"]
            self.assertTrue(runner.busy)
            app.run(timeout=30)
            self.assertIs(app.session_state["chat_tasks"], runner)
            verified.set()
            deadline = time.monotonic() + 2
            while runner.busy and time.monotonic() < deadline:
                runner.poll()
                time.sleep(0.01)
            app.run(timeout=30)
            self.assertEqual(len(app.exception), 0)
            self.assertEqual(len(runner.messages), 2)
            self.assertEqual(runner.messages[-1]["content"], "原任务完成")
            agent.invoke.assert_called_once()


if __name__ == "__main__":
    unittest.main()

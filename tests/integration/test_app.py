"""验证新旧入口、界面会话复用和工具边界；未安装界面依赖时跳过。"""
import importlib.util
from pathlib import Path
import unittest


def reply_stream(text):
    from langchain_core.messages import AIMessage

    return [("updates", {"model": {"messages": [AIMessage(content=text)]}})]


@unittest.skipUnless(importlib.util.find_spec("streamlit") and importlib.util.find_spec("langchain"), "未安装应用依赖")
class AppTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch
        from oa_agent.config import AppSettings, LoginConfig, ModelConfig
        from oa_agent.history.repository import InMemoryHistoryRepository

        # 所有界面测试均隔离本机 .env 和真实数据库。
        self.history_repository = InMemoryHistoryRepository()
        settings = patch("oa_agent.config.load_settings", return_value=AppSettings(LoginConfig(), ModelConfig()))
        repository = patch("oa_agent.history.mysql_repository.MySQLHistoryRepository", return_value=self.history_repository)
        settings.start()
        repository.start()
        self.addCleanup(settings.stop)
        self.addCleanup(repository.stop)

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
        self.assertEqual([button.label for button in app.sidebar.button][:3],
                         ["打开 OA 登录", "关闭会话并清除凭据", "新建会话"])
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

    def test_employee_tool_defaults_to_configured_account_without_credentials(self):
        from unittest.mock import Mock
        from oa_agent.tools.employee import make_employee_tool

        session = Mock()
        session.call.return_value = {"status": "auth_required", "status_code": 401, "message": "请重新登录"}
        tool = make_employee_tool(session)
        schema = tool.args_schema.model_json_schema()
        self.assertEqual(set(schema["properties"]), {"username"})
        self.assertNotIn("username", schema.get("required", []))
        self.assertEqual(tool.invoke({})["status_code"], 401)
        session.call.assert_called_once_with("user_info")

    def test_employee_tool_passes_named_account(self):
        from unittest.mock import Mock
        from oa_agent.tools.employee import make_employee_tool

        session = Mock()
        session.call.return_value = {"status": "success", "data": {"uname": "other.user"}}
        tool = make_employee_tool(session)
        self.assertEqual(tool.invoke({"username": "other.user"})["data"]["uname"], "other.user")
        session.call.assert_called_once_with("user_info", {"username": "other.user"})

    def test_employee_tool_null_account_uses_default(self):
        from unittest.mock import Mock
        from oa_agent.tools.employee import make_employee_tool

        session = Mock()
        make_employee_tool(session).invoke({"username": None})
        session.call.assert_called_once_with("user_info")

    def test_background_task_survives_app_rerun_and_delivers_once(self):
        import threading
        import time
        from unittest.mock import patch, Mock
        from streamlit.testing.v1 import AppTest
        from oa_agent.config import AppSettings, LoginConfig, ModelConfig

        verified = threading.Event()
        config = AppSettings(LoginConfig(), ModelConfig(api_key="dummy", model="test"))
        agent = Mock()
        agent.stream.side_effect = lambda *args, **kwargs: (verified.wait(2) or True) and reply_stream("原任务完成")
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
            agent.stream.assert_called_once()
            agent.invoke.assert_not_called()

    def history_app(self, agent=None):
        from unittest.mock import Mock, patch
        from streamlit.testing.v1 import AppTest
        from oa_agent.config import AppSettings, LoginConfig, ModelConfig

        config = AppSettings(LoginConfig(username="alice", password="dummy"),
                             ModelConfig(api_key="dummy", model="test"))
        agent = agent if agent is not None else Mock()
        if agent.stream.side_effect is None:
            agent.stream.side_effect = lambda data, **kwargs: reply_stream("回复：" + data["messages"][-1]["content"])
        for patcher in (patch("oa_agent.config.load_settings", return_value=config),
                        patch("oa_agent.agents.oa_agent.build_agent", return_value=agent)):
            patcher.start()
            self.addCleanup(patcher.stop)
        app = AppTest.from_file(str(Path(__file__).resolve().parents[2] / "apps" / "streamlit_app.py")).run(timeout=30)
        self.addCleanup(app.session_state["oa_session"].close)
        self.assertEqual(len(app.exception), 0)
        return app, agent

    def complete_chat(self, app):
        runner = app.session_state["chat_tasks"]
        if runner._future is not None:
            runner._future.result(timeout=5)
        app.run(timeout=30)
        self.assertEqual(len(app.exception), 0)

    def test_history_new_switch_followup_restart_and_confirmed_delete(self):
        app, agent = self.history_app()
        self.assertEqual(self.history_repository.list("alice"), [])
        app.chat_input[0].set_value("会话 A").run(timeout=30)
        self.complete_chat(app)
        first_id = app.session_state["chat_tasks"].history.current_id
        app.sidebar.button(key="new_conversation").click().run(timeout=30)
        self.assertEqual(app.session_state["chat_tasks"].messages, [])
        self.assertEqual(len(self.history_repository.list("alice")), 1)
        app.chat_input[0].set_value("会话 B").run(timeout=30)
        self.complete_chat(app)
        app.sidebar.button(key=f"open_{first_id}").click().run(timeout=30)
        app.chat_input[0].set_value("继续 A").run(timeout=30)
        self.complete_chat(app)
        self.assertEqual([message["content"] for message in agent.stream.call_args.args[0]["messages"]],
                         ["会话 A", "回复：会话 A", "继续 A"])
        restarted, _ = self.history_app()
        self.assertEqual(restarted.session_state["chat_tasks"].history.current_id, first_id)
        app.sidebar.button(key=f"delete_{first_id}").click().run(timeout=30)
        self.assertEqual(len(self.history_repository.list("alice")), 2)
        app.sidebar.button(key="cancel_delete_history").click().run(timeout=30)
        self.assertEqual(len(self.history_repository.list("alice")), 2)
        app.sidebar.button(key=f"delete_{first_id}").click().run(timeout=30)
        app.sidebar.button(key="confirm_delete_history").click().run(timeout=30)
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.session_state["chat_tasks"].messages, [])
        self.assertEqual(len(self.history_repository.list("alice")), 1)
        self.assertIn(first_id, self.history_repository.deleted)

    def test_history_switch_during_task_keeps_reply_in_original_and_disables_sending(self):
        import threading
        from unittest.mock import Mock

        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        agent = Mock()

        def respond(*args, **kwargs):
            started.set()
            release.wait(10)
            return reply_stream("后台完成")

        agent.stream.side_effect = respond
        app, _ = self.history_app(agent)
        app.chat_input[0].set_value("执行 A").run(timeout=30)
        self.assertTrue(started.wait(2))
        runner = app.session_state["chat_tasks"]
        first_id = runner.history.current_id
        self.assertTrue(app.sidebar.button(key=f"delete_{first_id}").disabled)
        app.sidebar.button(key="new_conversation").click().run(timeout=30)
        second_id = runner.history.current_id
        self.assertNotEqual(first_id, second_id)
        self.assertTrue(app.chat_input[0].disabled)
        self.assertEqual(runner.messages, [])
        release.set()
        self.complete_chat(app)
        self.assertEqual(runner.history.current_id, second_id)
        self.assertEqual(runner.messages, [])
        self.assertFalse(app.chat_input[0].disabled)
        app.sidebar.button(key=f"open_{first_id}").click().run(timeout=30)
        self.assertEqual(runner.messages[-1]["content"], "后台完成")
        self.assertFalse(app.sidebar.button(key=f"delete_{first_id}").disabled)
        agent.stream.assert_called_once()

    def test_unfinished_history_warns_without_starting_agent(self):
        from oa_agent.history.repository import ConversationSummary, HistoryMessage

        self.history_repository.create("alice", ConversationSummary("unfinished", "未完成", 1, 1))
        self.history_repository.append("alice", "unfinished", [HistoryMessage("prompt", "user", "待处理", 1)])
        app, agent = self.history_app()
        self.assertTrue(any("上次任务未完成" in warning.value for warning in app.warning))
        agent.stream.assert_not_called()

    def test_live_answer_and_summary_remain_visible_after_completion_and_switching(self):
        import threading
        from unittest.mock import Mock
        from langchain_core.messages import AIMessageChunk, ToolMessage

        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        agent = Mock()

        def chunks(*args, **kwargs):
            callback = kwargs["config"]["callbacks"][0]
            callback.on_tool_start({"name": "get_user_info"}, '{"username":"other.user","token":"private-input"}', run_id="tool1")
            callback.on_tool_end(ToolMessage(content='{"status":"success","data":"private-result"}',
                                             tool_call_id="tool1"), run_id="tool1")
            yield "messages", (AIMessageChunk(content=[{"type": "reasoning", "summary": [
                {"type": "summary_text", "text": "接口提供的推理摘要"}]}]), {})
            yield "messages", (AIMessageChunk(content="正在生成的回答"), {})
            started.set()
            release.wait(10)
            yield from reply_stream("最终回答")

        agent.stream.side_effect = chunks
        app, _ = self.history_app(agent)
        app.chat_input[0].set_value("查询我的部门").run(timeout=30)
        self.assertTrue(started.wait(2))
        app.run(timeout=30)
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(any(item.value == "正在生成的回答" for item in app.markdown))
        thinking = app.get("status")[0]
        self.assertTrue(thinking.label.startswith("思考中 ·"))
        self.assertTrue(thinking.proto.expanded)
        self.assertTrue(any(item.value == "接口提供的推理摘要" for item in app.markdown))
        self.assertTrue(any("查询用户信息" in item.value for item in app.caption))
        self.assertEqual(len(app.metric), 0)
        self.assertFalse(any("第 1 段" in item.value or "状态变化" in item.value for item in app.caption))
        self.assertFalse(any("private-" in item.value for item in app.markdown))
        first_id = app.session_state["chat_tasks"].history.current_id
        app.sidebar.button(key="new_conversation").click().run(timeout=30)
        self.assertFalse(any(item.value == "正在生成的回答" for item in app.markdown))
        self.assertEqual(len(app.get("status")), 0)
        release.set()
        self.complete_chat(app)
        app.sidebar.button(key=f"open_{first_id}").click().run(timeout=30)
        self.assertTrue(any(item.value == "最终回答" for item in app.markdown))
        self.assertTrue(any(item.value == "接口提供的推理摘要" for item in app.markdown))
        self.assertTrue(app.get("status")[0].label.startswith("已思考 ·"))
        self.assertFalse(app.get("status")[0].proto.expanded)
        self.assertEqual(len(app.get("status")), 1)
        agent.stream.assert_called_once()

    def test_stream_failure_shows_error_without_retrying_or_saving_partial_answer(self):
        from unittest.mock import Mock
        from langchain_core.messages import AIMessageChunk

        agent = Mock()

        def chunks(*args, **kwargs):
            yield "messages", (AIMessageChunk(content="不完整回答"), {})
            raise RuntimeError("private-token-in-exception")

        agent.stream.side_effect = chunks
        app, _ = self.history_app(agent)
        app.chat_input[0].set_value("查询").run(timeout=30)
        self.complete_chat(app)
        runner = app.session_state["chat_tasks"]
        self.assertEqual(runner.progress()[0].state, "error")
        self.assertIn("任务处理失败", runner.messages[-1]["content"])
        self.assertFalse(any("private-token" in item.value or "不完整回答" in item.value for item in app.markdown))
        self.assertEqual(len(app.get("status")), 1)
        self.assertTrue(app.get("status")[0].label.startswith("思考已中断 ·"))
        self.assertFalse(app.get("status")[0].proto.expanded)
        self.assertEqual(app.expander, [])
        agent.stream.assert_called_once()
        agent.invoke.assert_not_called()

    def test_sidebar_summary_switch_is_applied_to_next_model_call(self):
        from unittest.mock import patch

        app, _ = self.history_app()
        app.sidebar.toggle(key="request_reasoning_summary").set_value(True).run(timeout=30)
        with patch("oa_agent.agents.oa_agent.build_agent", return_value=self._fake_agent()) as build:
            app.chat_input[0].set_value("查询").run(timeout=30)
            self.complete_chat(app)
            self.assertTrue(build.call_args.args[1].reasoning_summary)
        self.assertTrue(any("本轮未收到模型推理摘要" in item.value for item in app.caption))

    @staticmethod
    def _fake_agent():
        from unittest.mock import Mock

        agent = Mock()
        agent.stream.side_effect = lambda *args, **kwargs: reply_stream("回答")
        return agent


if __name__ == "__main__":
    unittest.main()

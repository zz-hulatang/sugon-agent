"""使用真实 LangChain 图与本地模拟模型验证流式协议，不请求模型或 OA。"""
import json
import unittest
from unittest.mock import Mock
from uuid import uuid4

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeListChatModel, FakeMessagesListChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langchain_core.tools import tool

from oa_agent.agents.streaming import ProgressCallbacks, stream_agent_response
from oa_agent.agents.trace_details import input_details, result_details


class ToolModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class StreamingTests(unittest.TestCase):
    def test_real_graph_streams_text_and_model_lifecycle(self):
        agent = create_agent(model=FakeListChatModel(responses=["逐字生成回答"]), tools=[])
        events = []
        self.assertEqual(stream_agent_response(agent, [{"role": "user", "content": "test"}], events.append), "逐字生成回答")
        deltas = [event.text for event in events if event.kind == "answer_delta"]
        self.assertGreater(len(deltas), 1)
        self.assertEqual("".join(deltas), "逐字生成回答")
        self.assertTrue(any(event.kind == "step" and event.state == "complete" for event in events))

    def test_real_tool_loop_records_steps_without_raw_inputs_or_outputs(self):
        @tool
        def get_user_info(username: str) -> dict:
            """Return a fake employee."""
            return {"status": "success", "data": {"name": "private-profile", "token": "secret-token"}}

        agent = create_agent(model=ToolModel(responses=[
            AIMessage(content="先查询", tool_calls=[{"name": "get_user_info", "args": {"username": "other.user"}, "id": "call1"}]),
            AIMessage(content="最终回答"),
        ]), tools=[get_user_info])
        events = []
        self.assertEqual(stream_agent_response(agent, [{"role": "user", "content": "test"}], events.append), "最终回答")
        steps = [event for event in events if event.kind == "step"]
        self.assertTrue(any(event.text == "查询用户信息" and event.state == "running" for event in steps))
        self.assertTrue(any("查询用户信息" in event.text and event.state == "complete" for event in steps))
        self.assertTrue(any("查询目标：other.user" in event.details for event in steps))
        for value in ("private-profile", "secret-token"):
            self.assertNotIn(value, str(events))
        self.assertEqual([event.text for event in events if event.kind == "answer_set"][-1], "最终回答")
        self.assertTrue(any("操作说明：先查询" in event.details for event in steps))
        self.assertTrue(any("已选择工具操作" in event.text for event in steps))
        self.assertTrue(any(event.kind == "commentary" and event.text == "先查询" for event in events))

    def test_only_explicit_summary_is_displayed_and_not_duplicated_from_updates(self):
        content = [{"type": "reasoning", "summary": [{"type": "summary_text", "text": "简要分析"}],
                    "encrypted_content": "opaque-secret", "reasoning": "internal-reasoning"}]
        agent = Mock()
        agent.stream.return_value = iter([
            ("messages", (AIMessageChunk(content=content, additional_kwargs={"reasoning_content": "private-reasoning"}), {})),
            ("messages", (AIMessageChunk(content="最终回答"), {})),
            ("updates", {"model": {"messages": [AIMessage(content=[*content, {"type": "text", "text": "最终回答"}])]}}),
        ])
        events = []
        self.assertEqual(stream_agent_response(agent, [], events.append), "最终回答")
        self.assertEqual([event.text for event in events if event.kind == "summary_delta"], ["简要分析"])
        for value in ("opaque-secret", "internal-reasoning", "private-reasoning"):
            self.assertNotIn(value, str(events))

    def test_failed_tool_is_sanitized_and_marked_for_attention(self):
        events = []
        callback = ProgressCallbacks(events.append)
        run_id = uuid4()
        callback.on_tool_start({"name": "get_user_info"}, "secret-input", run_id=run_id)
        callback.on_tool_end(ToolMessage(content=json.dumps({"status": "auth_required", "message": "secret-output"}),
                                         tool_call_id="call1"), run_id=run_id)
        self.assertEqual(events[-1].state, "error")
        self.assertNotIn("secret", str(events))

    def test_stream_failure_does_not_reinvoke_agent_or_tool(self):
        agent = Mock()

        def chunks(*args, **kwargs):
            yield "messages", (AIMessageChunk(content="部分回答"), {})
            raise RuntimeError("secret-error")

        agent.stream.side_effect = chunks
        with self.assertRaises(RuntimeError):
            stream_agent_response(agent, [], lambda event: None)
        agent.stream.assert_called_once()
        agent.invoke.assert_not_called()

    def test_business_details_only_include_allowed_fields_and_counts(self):
        inputs = {"year": 2026, "month": 11, "token": "secret", "remark": "private-remark"}
        self.assertIn("查询范围：2026 年 11 月", input_details("get_attendance_exceptions", inputs))
        status, details = result_details("get_attendance_exceptions", {
            "status": "success", "message": "secret-response", "records": [{"name": "private-profile"}],
            "offset_types": ["病假", "公出"], "token": "secret",
        })
        self.assertEqual(status, "success")
        self.assertIn("异常记录：1 项", details)
        self.assertIn("可用处理类别：2 项", details)
        self.assertNotIn("secret", str(details))
        self.assertNotIn("private-profile", str(details))
        self.assertEqual(input_details("submit_abnormal_attendance", {
            "items": [{"remark": "private-remark", "record_id": "private-id"}]}),
            ("选定记录：1 条", "操作：提交 OA 审批"))
        for field in ("username", "password", "token"):
            self.assertNotIn("secret", str(input_details("oa_login", {"action": "status", field: "secret"})))

    def test_expired_and_changed_results_are_marked_as_needing_attention(self):
        for status in ("expired", "stale", "input_error"):
            with self.subTest(status=status):
                events = []
                callback = ProgressCallbacks(events.append)
                callback.on_tool_start({"name": "submit_abnormal_attendance"}, "{}", run_id="tool1")
                callback.on_tool_end({"status": status, "message": "private-message"}, run_id="tool1")
                self.assertEqual(events[-1].state, "error")
                self.assertNotIn("private-message", str(events))

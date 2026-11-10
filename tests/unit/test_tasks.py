import threading
import time
import unittest
from oa_agent.sessions.tasks import ChatTaskRunner
from oa_agent.schemas.progress import ProgressEvent


class ChatTaskTests(unittest.TestCase):
    def test_pending_task_survives_repeated_ui_polls(self):
        runner = ChatTaskRunner()
        verified = threading.Event()
        calls = []

        def respond(history):
            calls.append(history[0]["content"])
            verified.wait(2)
            return "原任务已完成"

        runner.submit("查询我的部门", respond)
        for _ in range(5):
            runner.poll()
        self.assertTrue(runner.busy)
        self.assertFalse(runner.submit("重复任务", respond))
        verified.set()
        deadline = time.monotonic() + 2
        while runner.busy and time.monotonic() < deadline:
            runner.poll()
            time.sleep(0.01)
        runner.poll()
        self.assertEqual(calls, ["查询我的部门"])
        self.assertEqual(len(runner.messages), 2)
        self.assertEqual(runner.messages[-1]["content"], "原任务已完成")

    def test_raw_exception_is_not_exposed(self):
        runner = ChatTaskRunner()
        runner.submit("test", lambda history: (_ for _ in ()).throw(RuntimeError("secret")))
        deadline = time.monotonic() + 2
        while runner.busy and time.monotonic() < deadline:
            runner.poll()
            time.sleep(0.01)
        self.assertNotIn("secret", runner.messages[-1]["content"])

    def test_streaming_snapshots_survive_reruns_and_are_isolated_by_conversation(self):
        runner = ChatTaskRunner()
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        first_id = runner.history.current_id
        emitters = []

        def respond(messages, emit):
            emitters.append(emit)
            emit(ProgressEvent("step", "tool1", "查询用户信息"))
            emit(ProgressEvent("summary_delta", text="接口提供的摘要"))
            emit(ProgressEvent("answer_delta", text="部分回答"))
            started.set()
            release.wait(3)
            emit(ProgressEvent("step", "tool1", "查询完成", "complete"))
            return "最终回答"

        runner.submit("查询", respond, streaming=True)
        self.assertTrue(started.wait(2))
        snapshot = runner.progress()[0]
        self.assertEqual(snapshot.answer, "部分回答")
        self.assertEqual(snapshot.summary, "接口提供的摘要")
        snapshot.steps.clear()
        self.assertEqual(len(runner.progress()[0].steps), 1)
        runner.history.new()
        self.assertEqual(runner.progress(), [])
        release.set()
        runner._future.result(timeout=3)
        runner.poll()
        self.assertEqual(runner.progress(), [])
        runner.history.select(first_id)
        completed = runner.progress()[0]
        self.assertEqual(completed.answer, "最终回答")
        self.assertEqual(completed.state, "complete")
        self.assertEqual(completed.steps[0].state, "complete")
        self.assertEqual([update.text for update in completed.steps[0].updates], ["查询用户信息", "查询完成"])
        emitters[0](ProgressEvent("answer_delta", text="迟到事件"))
        self.assertEqual(runner.progress()[0].answer, "最终回答")
        self.assertNotIn("接口提供的摘要", str(runner.messages))
        self.assertTrue(runner.delete_conversation(first_id))
        self.assertEqual(runner.progress(first_id), [])

    def test_failed_stream_replaces_partial_answer_and_finishes_pending_steps(self):
        runner = ChatTaskRunner()

        def respond(messages, emit):
            emit(ProgressEvent("step", "tool1", "查询中"))
            emit(ProgressEvent("answer_delta", text="未完成的回答"))
            raise RuntimeError("secret-token")

        runner.submit("查询", respond, streaming=True)
        runner._future.result(timeout=3)
        runner.poll()
        progress = runner.progress()[0]
        self.assertEqual(progress.state, "error")
        self.assertEqual(progress.steps[0].state, "error")
        self.assertEqual(progress.answer, runner.messages[-1]["content"])
        self.assertNotIn("secret-token", str(progress))
        self.assertNotIn("未完成的回答", runner.messages[-1]["content"])

    def test_details_and_model_summary_segments_are_preserved(self):
        runner = ChatTaskRunner()

        def respond(messages, emit):
            emit(ProgressEvent("step", "tool1", "查询中", category="tool", details=("查询目标：other.user",)))
            emit(ProgressEvent("step", "tool1", "查询完成", "complete", category="tool", details=("返回状态：成功",)))
            emit(ProgressEvent("summary_delta", key="model1", text="第一轮摘要"))
            emit(ProgressEvent("summary_delta", key="model2", text="第二轮摘要"))
            return "回答"

        runner.submit("查询", respond, streaming=True)
        runner._future.result(timeout=3)
        snapshot = runner.progress()[0]
        self.assertEqual(snapshot.steps[0].category, "tool")
        self.assertEqual(snapshot.steps[0].details, ("查询目标：other.user", "返回状态：成功"))
        self.assertEqual(list(snapshot.summaries.values()), ["第一轮摘要", "第二轮摘要"])
        self.assertEqual(len(snapshot.steps[0].updates), 2)

    def test_thinking_keeps_streamed_text_and_tools_in_occurrence_order(self):
        runner = ChatTaskRunner()

        def respond(messages, emit):
            emit(ProgressEvent("step", "model1", "模型运行中", category="model"))
            emit(ProgressEvent("summary_delta", key="model1", text="先确定"))
            emit(ProgressEvent("summary_delta", key="model1", text="查询范围。"))
            emit(ProgressEvent("commentary", key="note1", text="我将查询本月异常考勤。"))
            emit(ProgressEvent("step", "tool1", "查询异常考勤", category="tool"))
            emit(ProgressEvent("step", "tool1", "查询完成", "complete", category="tool"))
            emit(ProgressEvent("summary_delta", key="model2", text="依据查询结果整理答复。"))
            return "回答"

        runner.submit("查询", respond, streaming=True)
        runner._future.result(timeout=3)
        snapshot = runner.progress()[0]
        self.assertEqual([(entry.kind, entry.key) for entry in snapshot.thinking], [
            ("summary", "model1"), ("commentary", "note1"), ("tool", "tool1"), ("summary", "model2")])
        self.assertEqual(snapshot.thinking[0].text, "先确定查询范围。")
        self.assertEqual(snapshot.thinking[-1].text, "依据查询结果整理答复。")
        self.assertNotIn("查询范围", snapshot.answer)

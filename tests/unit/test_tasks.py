import threading
import time
import unittest
from oa_agent.sessions.tasks import ChatTaskRunner


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

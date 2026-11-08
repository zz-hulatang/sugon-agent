"""历史管理的持久化恢复、离线补存、删除和后台任务归属。"""
import threading
import time
import unittest
from unittest.mock import patch

from oa_agent.history.manager import ConversationHistory
from oa_agent.history.repository import (
    ConversationDeleted, ConversationSummary, HistoryError, HistoryMessage, InMemoryHistoryRepository,
)
from oa_agent.sessions.tasks import ChatTaskRunner


def finish(runner):
    deadline = time.monotonic() + 3
    while runner.busy and time.monotonic() < deadline:
        runner.poll()
        time.sleep(0.005)
    if runner.busy:
        raise AssertionError("后台任务未完成")


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.repo = InMemoryHistoryRepository()
        self.history = ConversationHistory("alice", self.repo)

    def test_lazy_create_title_restore_context_and_account_isolation(self):
        self.history.new()
        self.assertEqual(self.repo.list("alice"), [])
        conversation_id = self.history.current_id
        self.history.add(conversation_id, "user", "  查询\n 我的部门  " + "问" * 35)
        self.history.add(conversation_id, "assistant", "研发部")
        self.history.flush()
        other = ConversationHistory("alice", self.repo)
        self.assertTrue(other.refresh())
        self.assertEqual(other.current_id, conversation_id)
        self.assertEqual(other.messages(), self.history.messages())
        self.assertEqual(other.summaries[0].title, ("查询 我的部门 " + "问" * 35)[:30])
        self.assertEqual(self.repo.list("bob"), [])
        for operation in (lambda: self.repo.load("bob", conversation_id),
                          lambda: self.repo.append("bob", conversation_id, []),
                          lambda: self.repo.delete("bob", conversation_id),
                          lambda: self.repo.create("bob", other.summaries[0])):
            with self.assertRaises(ConversationDeleted):
                operation()

    def test_pagination_and_most_recent_first(self):
        for index in range(65):
            self.repo.create("alice", ConversationSummary(str(index), str(index), index, index))
        self.history.refresh()
        self.assertEqual(len(self.history.summaries), 30)
        self.assertEqual(self.history.summaries[0].id, "64")
        self.assertTrue(self.history.has_more)
        self.history.load_more()
        self.assertEqual(len(self.history.summaries), 60)
        self.history.load_more()
        self.assertEqual(len(self.history.summaries), 65)
        self.assertFalse(self.history.has_more)

    def test_save_failure_keeps_messages_and_lost_ack_retry_is_idempotent(self):
        conversation_id = self.history.current_id
        self.history.add(conversation_id, "user", "问题")
        original = self.repo.append

        def lost_ack(*args):
            original(*args)
            raise HistoryError("连接中断")

        with patch.object(self.repo, "append", side_effect=lost_ack):
            self.history.flush()
        self.assertIn("尚未保存", self.history.warning)
        self.history.add(conversation_id, "assistant", "回答")
        self.history.flush()
        self.assertEqual(self.history.warning, "")
        self.assertEqual([m.content for m in self.repo.load("alice", conversation_id).messages], ["问题", "回答"])

    def test_read_and_delete_failure_keep_current_content(self):
        conversation_id = self.history.current_id
        self.history.add(conversation_id, "user", "保留此问题")
        with patch.object(self.repo, "load", side_effect=HistoryError("读取失败")):
            self.assertFalse(self.history.select("other"))
        self.assertEqual(self.history.current_id, conversation_id)
        with patch.object(self.repo, "create", side_effect=HistoryError("数据库断开")):
            self.assertFalse(self.history.delete(conversation_id))
        self.assertEqual(self.history.messages()[0]["content"], "保留此问题")
        self.assertIn("删除未完成", self.history.warning)

    def test_delete_clears_body_and_tombstone_blocks_old_retry(self):
        conversation_id = self.history.current_id
        self.history.add(conversation_id, "user", "问题")
        self.history.flush()
        old_summary = self.history.summaries[0]
        self.assertTrue(self.history.delete(conversation_id))
        self.assertEqual(self.history.messages(), [])
        self.assertEqual(self.repo.rows[conversation_id][1].messages, [])
        with self.assertRaises(ConversationDeleted):
            self.repo.create("alice", old_summary)
        with self.assertRaises(ConversationDeleted):
            self.repo.append("alice", conversation_id, [HistoryMessage("retry", "assistant", "回答", 1)])

    def test_other_page_deletion_discards_pending_save_without_resurrecting(self):
        conversation_id = self.history.current_id
        self.history.add(conversation_id, "user", "问题")
        self.history.flush()
        self.history.add(conversation_id, "assistant", "待保存")
        self.repo.delete("alice", conversation_id)
        self.history.flush()
        self.assertEqual(self.repo.list("alice"), [])
        self.assertEqual(self.history.messages(), [])
        self.assertIn("已在其他页面删除", self.history.warning)

    def test_background_reply_persists_to_original_without_polling_and_followup_is_isolated(self):
        runner = ChatTaskRunner(self.history)
        started, release = threading.Event(), threading.Event()
        first_id = self.history.current_id
        calls = []

        def respond(messages):
            calls.append(messages)
            started.set()
            release.wait(3)
            return "原会话回复"

        runner.submit("原会话问题", respond)
        self.assertTrue(started.wait(2))
        self.assertFalse(runner.delete_conversation(first_id))
        second_id = self.history.new()
        self.assertFalse(runner.submit("并发问题", respond))
        release.set()
        runner._future.result(timeout=3)  # 未调用 poll，后台仍必须完成落库。
        self.assertEqual(self.repo.load("alice", first_id).messages[-1].content, "原会话回复")
        self.assertEqual(self.history.current_id, second_id)
        self.assertEqual(runner.messages, [])
        finish(runner)
        runner.submit("新会话问题", lambda messages: calls.append(messages) or "新回复")
        finish(runner)
        self.assertEqual(calls[-1], [{"role": "user", "content": "新会话问题"}])
        self.history.select(first_id)
        runner.submit("继续原问题", lambda messages: calls.append(messages) or "续聊回复")
        finish(runner)
        self.assertEqual([m["content"] for m in calls[-1]], ["原会话问题", "原会话回复", "继续原问题"])

    def test_offline_task_executes_once_then_background_retry_only_saves(self):
        runner = ChatTaskRunner(self.history)
        calls = []
        with patch.object(self.repo, "create", side_effect=HistoryError("暂时不可用")):
            runner.submit("问题", lambda history: calls.append(history) or "回答")
            finish(runner)
        self.assertEqual(len(calls), 1)
        self.assertTrue(self.history.retry(force=True))
        deadline = time.monotonic() + 3
        while self.history._retry_running and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(len(self.repo.load("alice", self.history.current_id).messages), 2)
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.history.retry())

    def test_deleted_during_execution_does_not_leave_runner_busy(self):
        runner = ChatTaskRunner(self.history)
        release, started = threading.Event(), threading.Event()
        conversation_id = self.history.current_id

        def respond(messages):
            started.set()
            release.wait(3)
            return "完成"

        runner.submit("问题", respond)
        self.assertTrue(started.wait(2))
        self.repo.delete("alice", conversation_id)
        release.set()
        finish(runner)
        self.assertEqual(self.repo.list("alice"), [])

    def test_automatic_retry_saves_without_ui_polls(self):
        self.history.RETRY_SECONDS = 0.03
        saved = threading.Event()
        original_append = self.repo.append

        def append(*args):
            original_append(*args)
            saved.set()

        runner = ChatTaskRunner(self.history)
        with patch.object(self.repo, "create", side_effect=HistoryError("离线")):
            runner.submit("问题", lambda messages: "回答")
            runner._future.result(timeout=3)
        with patch.object(self.repo, "append", side_effect=append):
            self.assertTrue(saved.wait(2))
        self.assertEqual(len(self.repo.load("alice", self.history.current_id).messages), 2)

    def test_unfinished_history_is_restored_without_replay(self):
        self.history.add(self.history.current_id, "user", "查询待完成")
        self.history.flush()
        restored = ConversationHistory("alice", self.repo)
        restored.refresh()
        runner = ChatTaskRunner(restored)
        self.assertFalse(runner.busy)
        self.assertEqual(runner.messages[-1]["role"], "user")

"""MySQL 历史仓库的加密绑定、事务失败和参数隔离检查。"""
import unittest
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet

from oa_agent.config import MySQLConfig
from oa_agent.history.mysql_repository import MySQLHistoryRepository
from oa_agent.history.repository import ConversationDeleted, ConversationSummary, HistoryError, HistoryMessage


class MySQLHistoryTests(unittest.TestCase):
    def setUp(self):
        self.key = Fernet.generate_key().decode()
        self.repo = MySQLHistoryRepository(MySQLConfig(user="test", encryption_key=self.key))
        self.connection, self.cursor = Mock(), Mock()
        self.connection.cursor.return_value.__enter__ = Mock(return_value=self.cursor)
        self.connection.cursor.return_value.__exit__ = Mock(return_value=False)
        patcher = patch("oa_agent.history.mysql_repository.pymysql.connect", return_value=self.connection)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_encrypted_roundtrip_and_payload_binding(self):
        message = HistoryMessage("message", "user", "员工私密信息", 123)
        self.cursor.fetchone.side_effect = [{"id": "conversation"}, {"last_number": 0}, None]
        self.repo.append("alice", "conversation", [message])
        inserts = [call.args for call in self.cursor.execute.call_args_list if "INSERT INTO oa_chat_messages" in call.args[0]]
        payload = inserts[0][1][4]
        self.assertNotIn(message.content.encode(), payload)
        self.cursor.fetchone.side_effect = None
        self.cursor.fetchone.return_value = dict(id="conversation", title="标题", created_at=1, updated_at=123)
        self.cursor.fetchall.return_value = [dict(id="message", role="user", payload=payload, created_at=123)]
        self.assertEqual(self.repo.load("alice", "conversation").messages, [message])
        with self.assertRaises(HistoryError):
            self.repo.load("bob", "conversation")
        self.connection.rollback.assert_called()

    def test_parameterized_owner_queries_and_tombstone_check(self):
        self.cursor.fetchone.return_value = None
        with self.assertRaises(ConversationDeleted):
            self.repo.create("alice", ConversationSummary("conversation", "title", 1, 1))
        sql, args = self.cursor.execute.call_args.args
        self.assertIn("username=%s", sql)
        self.assertIn("deleted_at IS NULL", sql)
        self.assertEqual(args, ("conversation", "alice"))
        self.connection.rollback.assert_called_once()

    def test_delete_clears_body_and_marks_deleted_in_one_transaction(self):
        self.cursor.fetchone.return_value = {"id": "conversation"}
        self.repo.delete("alice", "conversation")
        statements = [call.args[0] for call in self.cursor.execute.call_args_list]
        self.assertTrue(any("DELETE FROM oa_chat_messages" in sql for sql in statements))
        self.assertTrue(any("deleted_at=COALESCE" in sql for sql in statements))
        self.connection.commit.assert_called_once()
        self.connection.rollback.assert_not_called()

    def test_raw_database_errors_and_cleanup_errors_are_not_exposed(self):
        self.cursor.execute.side_effect = RuntimeError("secret-password")
        self.connection.rollback.side_effect = RuntimeError("secret-address")
        self.connection.close.side_effect = RuntimeError("secret-token")
        with self.assertRaises(HistoryError) as error:
            self.repo.list("alice")
        self.assertNotIn("secret", str(error.exception))

    def test_decryption_failure_is_safe_and_specific(self):
        self.cursor.fetchone.return_value = dict(id="conversation", title="title", created_at=1, updated_at=1)
        self.cursor.fetchall.return_value = [dict(id="message", role="assistant", payload=b"invalid", created_at=1)]
        with self.assertRaises(HistoryError) as error:
            self.repo.load("alice", "conversation")
        self.assertIn("无法解密", str(error.exception))

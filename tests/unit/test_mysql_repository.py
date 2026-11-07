"""MySQL 仓库的加密、按用户隔离和乐观失效删除检查。"""
import unittest
import pymysql
from unittest.mock import Mock, patch
from cryptography.fernet import Fernet

from oa_agent.config import MySQLConfig
from oa_agent.schemas.auth import Credentials
from oa_agent.sessions.mysql_repository import MySQLSessionRepository
from oa_agent.sessions.repository import SessionRepositoryError


class MySQLRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.config = MySQLConfig(user="test", password="dummy", encryption_key=Fernet.generate_key().decode())
        self.repo = MySQLSessionRepository(self.config)
        self.cursor = Mock()
        self.connection = Mock()
        self.connection.cursor.return_value.__enter__ = Mock(return_value=self.cursor)
        self.connection.cursor.return_value.__exit__ = Mock(return_value=False)

    def test_encrypted_payload_round_trip_and_metadata_binding(self):
        credentials = Credentials(cookies=[{"name": "SESSION", "value": "cookie-secret"}],
                                  tokens={"header:token": "token-secret"}, captured_at=123, verified=True)
        with patch("oa_agent.sessions.mysql_repository.pymysql.connect", return_value=self.connection):
            saved = self.repo.save("demo", credentials, "token-secret")
            sql, args = self.cursor.execute.call_args.args
            self.assertIn("ON DUPLICATE KEY UPDATE", sql)
            self.assertNotIn(b"token-secret", args[1])
            self.assertNotIn(b"cookie-secret", args[1])
            self.cursor.fetchone.return_value = {"payload": args[1], "revision": saved.revision}
            loaded = self.repo.load("demo")
            self.assertEqual(loaded.token, "token-secret")
            self.assertEqual(loaded.credentials.cookies, credentials.cookies)
            with self.assertRaises(SessionRepositoryError):
                self.repo.load("other-user")

    def test_conditional_delete_uses_username_and_revision_parameters(self):
        with patch("oa_agent.sessions.mysql_repository.pymysql.connect", return_value=self.connection):
            self.repo.delete("demo", "revision-1")
            sql, args = self.cursor.execute.call_args.args
            self.assertIn("revision=%s", sql)
            self.assertEqual(args, ("demo", "revision-1"))

    def test_database_error_is_sanitized(self):
        with patch("oa_agent.sessions.mysql_repository.pymysql.connect", side_effect=RuntimeError("dummy-password")):
            with self.assertRaises(SessionRepositoryError) as error:
                self.repo.load("demo")
            self.assertNotIn("dummy-password", str(error.exception))

    def test_mysql_error_code_produces_safe_specific_diagnosis(self):
        for number, reason in ((1049, "database_not_found"), (1045, "access_denied"), (2003, "connection_failed"), (1142, "permission_denied")):
            with self.subTest(number=number), patch("oa_agent.sessions.mysql_repository.pymysql.connect", side_effect=pymysql.OperationalError(number, "secret-connection-details")):
                with self.assertRaises(SessionRepositoryError) as error:
                    self.repo.load("demo")
                self.assertEqual(error.exception.code, reason)
                self.assertNotIn("secret-connection-details", error.exception.public_message)

    def test_decryption_failure_has_specific_safe_message(self):
        self.cursor.fetchone.return_value = {"payload": b"not-valid-ciphertext", "revision": "revision"}
        with patch("oa_agent.sessions.mysql_repository.pymysql.connect", return_value=self.connection):
            with self.assertRaises(SessionRepositoryError) as error:
                self.repo.load("demo")
            self.assertEqual(error.exception.code, "decrypt_failed")

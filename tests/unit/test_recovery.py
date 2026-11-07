"""401 恢复时保留同一个任务，包含等待人工验证和用户取消。"""
import threading
import time
import unittest
from unittest.mock import Mock

from oa_agent.clients.oa_client import OaApiError
from oa_agent.config import LoginConfig
from oa_agent.schemas.auth import Credentials
from oa_agent.sessions.repository import InMemorySessionRepository
from oa_agent.sessions.session import LoginSession


class RecoveryTests(unittest.TestCase):
    def create_session(self):
        repo = InMemorySessionRepository()
        repo.save("demo", Credentials(tokens={"header:token": "old"}, verified=True), "old")
        session = LoginSession(LoginConfig(username="demo", password="dummy", relogin_timeout=3), repository=repo)
        self.addCleanup(session.close)
        entered, verified = threading.Event(), threading.Event()
        session.browser.close = Mock(side_effect=lambda: setattr(session.browser, "context", None))

        def open_browser(*args):
            session.browser.context = object()
            entered.set()

        session.browser.open = Mock(side_effect=open_browser)
        session.browser.pump_events = Mock(side_effect=lambda: (time.sleep(0.01) or True))

        def login_tick():
            if verified.is_set():
                session.store.credentials = Credentials(tokens={"header:token": "fresh"}, verified=True)
                session.store.state = "verified"
                session._publish()
            else:
                session.store.state = "waiting_login"
                session.auto_login.state = "needs_verification"

        session._login_tick = login_tick
        calls = []

        def fetch(username, token, cookies=None):
            calls.append(token)
            if token == "old":
                raise OaApiError(401)
            return {"uname": "demo", "name2": "测试用户"}

        session.employee.client.get_employee_detail = Mock(side_effect=fetch)
        return session, repo, entered, verified, calls

    def test_manual_mfa_completion_resumes_original_future(self):
        session, repo, entered, verified, calls = self.create_session()
        results = []
        thread = threading.Thread(target=lambda: results.append(session.call("user_info")))
        thread.start()
        self.assertTrue(entered.wait(2))
        self.assertTrue(session.call("status")["recovering"])
        self.assertTrue(thread.is_alive())
        verified.set()  # 模拟用户在浏览器中完成 MFA，未再次调用 user_info。
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0]["status"], "success")
        self.assertEqual(calls, ["old", "fresh"])
        self.assertEqual(repo.load("demo").token, "fresh")

    def test_user_clear_cancels_pending_task(self):
        session, repo, entered, _, calls = self.create_session()
        results = []
        thread = threading.Thread(target=lambda: results.append(session.call("user_info")))
        thread.start()
        self.assertTrue(entered.wait(2))
        session.call("clear")
        thread.join(timeout=2)
        self.assertEqual(results[0]["status"], "auth_required")
        self.assertIsNone(repo.load("demo"))
        self.assertEqual(calls, ["old"])

    def test_shared_user_newer_row_is_not_deleted_by_old_401(self):
        session, repo, _, _, calls = self.create_session()

        def fetch(username, token, cookies=None):
            calls.append(token)
            if token == "old":
                repo.save("demo", Credentials(verified=True), "newer")
                raise OaApiError(401)
            return {"uname": "demo"}

        session.employee.client.get_employee_detail.side_effect = fetch
        session.requests.renew_login = Mock()
        self.assertEqual(session.call("user_info")["status"], "success")
        session.requests.renew_login.assert_not_called()
        self.assertEqual(calls, ["old", "newer"])

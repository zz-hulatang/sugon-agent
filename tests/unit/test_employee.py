"""使用虚构员工信息验证 API 认证、业务返回和 401 会话清理。"""
import json
from pathlib import Path
import threading
import unittest
from contextlib import nullcontext
from unittest.mock import Mock

from oa_agent.clients.oa_client import OaApiError, OaClient
from oa_agent.config import LoginConfig
from oa_agent.schemas.auth import Credentials
from oa_agent.services.employee import EmployeeService
from oa_agent.sessions.session import LoginSession
from oa_agent.sessions.store import CredentialStore
from oa_agent.sessions.repository import InMemorySessionRepository, SessionRepositoryError
from oa_agent.services.authenticated_request import AuthenticatedRequestService, LoginRecoveryError


PROFILE = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "employee_detail.json").read_text())


class OaClientTests(unittest.TestCase):
    def setUp(self):
        self.response = Mock(status=200)
        self.response.json.return_value = dict(PROFILE)
        self.browser = Mock(context=Mock())
        self.browser.context.request.get.return_value = self.response
        self.browser.request_context.return_value = nullcontext(self.browser.context.request)
        self.client = OaClient(self.browser)

    def test_success_keeps_original_fields_and_uses_context_cookie_jar(self):
        data = self.client.get_employee_detail("demo.user", "test-token", cookies=[])
        self.assertEqual(data, PROFILE)
        self.assertEqual(data["pernr"], "00000001")
        self.browser.context.request.get.assert_called_once_with(
            "https://msp.soa.com.cn/api/sap/getEmployeeDetail/demo.user",
            headers={"token": "test-token"}, timeout=15000, max_redirects=0, fail_on_status_code=False,
        )
        self.response.dispose.assert_called_once()

    def test_username_is_encoded_as_one_path_segment(self):
        self.client.get_employee_detail("demo/a?b#c", "test-token", cookies=[])
        url = self.browser.context.request.get.call_args.args[0]
        self.assertTrue(url.endswith("demo%2Fa%3Fb%23c"))

    def test_http_failure_disposes_response_and_preserves_only_status(self):
        for status in (401, 403, 302, 500):
            with self.subTest(status=status):
                self.response.status = status
                with self.assertRaises(OaApiError) as error:
                    self.client.get_employee_detail("demo.user", "test-token", cookies=[])
                self.assertEqual(error.exception.status_code, status)
                self.assertNotIn("test-token", str(error.exception))
                self.response.json.assert_not_called()
        self.assertEqual(self.response.dispose.call_count, 4)

    def test_invalid_json_and_unexpected_data_are_errors(self):
        for data in (None, [], {}, {"uname": 1}, {"uname": ""}):
            with self.subTest(data=data), self.assertRaises(OaApiError):
                self.response.json.return_value = data
                self.client.get_employee_detail("demo.user", "test-token", cookies=[])
        self.response.json.side_effect = ValueError("invalid JSON")
        with self.assertRaises(OaApiError):
            self.client.get_employee_detail("demo.user", "test-token", cookies=[])

    def test_network_exception_is_sanitized(self):
        self.browser.context.request.get.side_effect = RuntimeError("token=test-token")
        with self.assertRaises(OaApiError) as error:
            self.client.get_employee_detail("demo.user", "test-token", cookies=[])
        self.assertNotIn("test-token", str(error.exception))


class EmployeeServiceTests(unittest.TestCase):
    def setUp(self):
        self.config = LoginConfig(username="demo.user", password="dummy-password")
        self.repository = InMemorySessionRepository()
        self.credentials = Credentials(tokens={"header:token": "test-token"}, verified=True)
        self.repository.save("demo.user", self.credentials, "test-token")
        self.client = Mock()
        self.client.get_employee_detail.return_value = dict(PROFILE)
        self.invalidate = Mock()
        self.renew = Mock(side_effect=lambda: self.repository.save("demo.user", self.credentials, "new-token"))
        self.requests = AuthenticatedRequestService("demo.user", self.repository, self.renew, self.invalidate)
        self.service = EmployeeService(self.config, self.client, self.requests)

    def test_returns_full_profile_using_repository_credentials(self):
        result = self.service.get_current_user()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["data"], PROFILE)
        self.client.get_employee_detail.assert_called_once_with("demo.user", "test-token", cookies=[])
        self.assertNotIn("test-token", json.dumps(result))

    def test_unconfigured_user_does_not_call_api(self):
        self.service.config = LoginConfig()
        self.assertEqual(self.service.get_current_user()["status"], "config_error")
        self.client.get_employee_detail.assert_not_called()

    def test_missing_record_logs_in_and_continues_same_task(self):
        self.repository.delete("demo.user")
        self.assertEqual(self.service.get_current_user()["status"], "success")
        self.renew.assert_called_once()
        self.assertEqual(self.client.get_employee_detail.call_args.args[1], "new-token")

    def test_401_relogs_in_and_resumes_without_second_user_prompt(self):
        self.client.get_employee_detail.side_effect = [OaApiError(401), dict(PROFILE)]
        result = self.service.get_current_user()
        self.assertEqual(result["status"], "success")
        self.renew.assert_called_once()
        self.assertEqual([call.args[1] for call in self.client.get_employee_detail.call_args_list], ["test-token", "new-token"])

    def test_second_401_stops_retry_and_removes_invalid_record(self):
        self.client.get_employee_detail.side_effect = OaApiError(401)
        result = self.service.get_current_user()
        self.assertEqual(result["status_code"], 401)
        self.renew.assert_called_once()
        self.assertEqual(self.client.get_employee_detail.call_count, 2)
        self.assertIsNone(self.repository.load("demo.user"))

    def test_database_failure_does_not_use_memory_or_start_login(self):
        self.repository.load = Mock(side_effect=SessionRepositoryError("offline"))
        self.assertEqual(self.service.get_current_user()["status"], "error")
        self.client.get_employee_detail.assert_not_called()
        self.renew.assert_not_called()

    def test_database_diagnosis_is_returned_without_raw_exception(self):
        self.repository.load = Mock(side_effect=SessionRepositoryError("secret", code="database_not_found"))
        result = self.service.get_current_user()
        self.assertIn("数据库不存在", result["message"])
        self.assertNotIn("secret", result["message"])

    def test_login_failure_does_not_send_request(self):
        self.repository.delete("demo.user")
        self.renew.side_effect = LoginRecoveryError("timeout")
        self.assertEqual(self.service.get_current_user()["status"], "auth_required")
        self.client.get_employee_detail.assert_not_called()

    def test_other_http_failure_does_not_relogin(self):
        self.client.get_employee_detail.side_effect = OaApiError(500)
        self.assertEqual(self.service.get_current_user()["status"], "error")
        self.renew.assert_not_called()

    def test_different_account_is_not_returned(self):
        self.client.get_employee_detail.return_value = {"uname": "other.user"}
        self.assertEqual(self.service.get_current_user()["status"], "error")

    def test_worker_uses_db_record_even_when_memory_is_empty(self):
        session = LoginSession(self.config, repository=self.repository)
        self.addCleanup(session.close)
        session.requests.renew_login = self.renew
        executed = []

        def fetch(username, token, cookies=None):
            executed.append(threading.get_ident())
            return dict(PROFILE)

        session.employee.client.get_employee_detail = Mock(side_effect=fetch)
        self.assertEqual(session.call("user_info")["status"], "success")
        self.assertNotEqual(executed[0], threading.get_ident())
        session.employee.client.get_employee_detail.side_effect = OaApiError(401)
        self.assertEqual(session.call("user_info")["status_code"], 401)
        self.assertEqual(session.call("status")["state"], "expired")

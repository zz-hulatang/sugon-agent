"""异常考勤的身份约束、快照校验、只读恢复和写请求不重放。全部数据虚构。"""
from contextlib import nullcontext
from copy import deepcopy
from datetime import date
import json
import threading
import unittest
from unittest.mock import Mock

from pydantic import ValidationError

from oa_agent.clients.attendance_client import AttendanceClient, AttendancePageError, flatten_records
from oa_agent.clients.oa_client import OaApiError
from oa_agent.config import LoginConfig
from oa_agent.schemas.attendance import AttendanceCorrection
from oa_agent.schemas.auth import Credentials
from oa_agent.services.attendance import AttendanceService
from oa_agent.services.authenticated_request import AuthenticatedRequestService
from oa_agent.sessions.repository import InMemorySessionRepository, SessionRepositoryError
from oa_agent.sessions.session import LoginSession
from oa_agent.tools.attendance import make_attendance_tools


ROW = {"rq": f"{date.today().year}-11-02", "dk": "09:05:15", "dk2": "18:26:32",
       "lx": "旷工", "qjxs": "0.08", "qjt": "0.01", "workday": True,
       "dxlb": "", "dxlbbm": "", "internal": "not-for-model"}
CHOICES = [{"type": "公出", "typecode": "A008"}, {"type": "忘打卡", "typecode": "A013"},
           {"type": "婚假", "typecode": "A004"}]


class AttendanceClientTests(unittest.TestCase):
    def setUp(self):
        self.request = Mock()
        self.browser = Mock()
        self.browser.request_context.side_effect = lambda cookies: nullcontext(self.request)
        self.client = AttendanceClient(self.browser)

    def response(self, data, status=200, content_type="application/json"):
        response = Mock(status=status, headers={"content-type": content_type})
        response.json.return_value = data
        return response

    def test_real_parameter_spelling_and_hire_date_cutoff(self):
        identity = self.response([{"USRID": "demo.user", "ZZGZD": "001", "BEGDA": "2026-02-10"}])
        records = self.response({"code": 200, "data": {"month": [ROW, dict(ROW, qjxs=0)]}})
        options = self.response({"code": 200, "data": {"records": CHOICES}})
        self.request.post.return_value = identity
        self.request.get.side_effect = [records, options]
        result = self.client.query("demo.user", 2026, 2, [])
        self.assertEqual(result["records"], [ROW])
        params = self.request.get.call_args_list[0].kwargs["params"]
        self.assertEqual(params, {"starTime": "2026-02-10", "endTime": "2026-02-28",
                                  "workplaceid": "001", "itcode": "demo.user", "postflag": "abnormalattendapp"})
        self.assertEqual(self.request.post.call_args.kwargs["form"]["type"], "getLoginPsn")
        for response in [identity, records, options]:
            response.dispose.assert_called_once()

    def test_http_200_login_html_is_auth_failure_after_content_check(self):
        response = self.response({}, content_type="text/html;charset=UTF-8")
        response.json.side_effect = ValueError("not JSON")
        response.text.return_value = '<html><form name="loginform"></form></html>'
        self.request.get.return_value = response
        with self.assertRaises(OaApiError) as error:
            self.client._json(self.request, "/appForm/GetViewData")
        self.assertEqual(error.exception.status_code, 401)
        response.json.assert_called_once()
        response.dispose.assert_called_once()

    def test_json_mislabeled_as_html_is_valid_and_other_html_does_not_relogin(self):
        response = self.response([{"USRID": "demo.user"}], content_type="text/html;charset=UTF-8")
        self.request.get.return_value = response
        self.assertEqual(self.client._json(self.request, "/test"), [{"USRID": "demo.user"}])
        response.json.side_effect = ValueError("not JSON")
        response.text.return_value = "<html>service unavailable</html>"
        with self.assertRaises(OaApiError) as error:
            self.client._json(self.request, "/test")
        self.assertEqual(error.exception.status_code, 200)

    def test_redirect_and_explicit_business_401_are_not_success(self):
        for response in [self.response({}, status=302), self.response({"code": 401})]:
            self.request.get.return_value = response
            with self.assertRaises(OaApiError) as error:
                self.client._json(self.request, "/test")
            self.assertEqual(error.exception.status_code, 401)
        self.assertEqual(self.request.get.call_args.kwargs["max_redirects"], 0)

    def test_identity_mismatch_stops_before_attendance_query(self):
        self.request.post.return_value = self.response([{"USRID": "other", "ZZGZD": "001"}])
        with self.assertRaises(AttendancePageError):
            self.client.query("demo.user", 2026, 11, [])
        self.request.get.assert_not_called()

    def test_previous_period_and_malformed_numeric_values_are_blocked(self):
        with self.assertRaises(AttendancePageError):
            flatten_records({"code": 200, "data": {"lastNeedEndDate": "2026-10-31"}})
        for bad in [dict(ROW, qjt="NaN"), dict(ROW, qjxs="Infinity"), dict(ROW, rq="invalid")]:
            with self.assertRaises(OaApiError):
                flatten_records({"code": 200, "data": {"month": [bad]}})

    def test_raw_network_errors_are_sanitized(self):
        self.request.get.side_effect = RuntimeError("Cookie=secret")
        with self.assertRaises(OaApiError) as error:
            self.client._json(self.request, "/test")
        self.assertNotIn("secret", str(error.exception))


class AttendanceServiceTests(unittest.TestCase):
    def setUp(self):
        self.config = LoginConfig(username="demo.user", password="dummy")
        self.repository = InMemorySessionRepository()
        self.repository.save("demo.user", Credentials(cookies=[], verified=True), "secret-token")
        self.client = Mock()
        self.client.query.side_effect = lambda *args: deepcopy({"records": [ROW], "choices": CHOICES})
        self.client.submit.return_value = {"doc_id": "test-doc", "serial_number": "TEST-1"}
        self.renew = Mock(side_effect=lambda: self.repository.save(
            "demo.user", Credentials(cookies=[], verified=True), "new-secret"))
        self.requests = AuthenticatedRequestService("demo.user", self.repository, self.renew, Mock())
        self.service = AttendanceService(self.config, self.client, self.requests)

    def draft(self):
        result = self.service.query(date.today().year, 11)
        self.assertEqual(result["status"], "success")
        return result["draft_id"], {"record_id": result["records"][0]["record_id"], "offset_type": "公出", "remark": "客户现场"}

    def test_query_returns_only_business_fields_and_does_not_submit(self):
        result = self.service.query(date.today().year, 11)
        self.assertEqual(result["offset_types"], [item["type"] for item in CHOICES])
        self.assertNotIn("not-for-model", json.dumps(result))
        self.assertNotIn("secret-token", json.dumps(result))
        self.client.submit.assert_not_called()

    def test_invalid_month_or_unconfigured_identity_does_not_query(self):
        self.assertEqual(self.service.query(date.today().year, 13)["status"], "input_error")
        self.assertEqual(self.service.query(True, 11)["status"], "input_error")
        self.service.config = LoginConfig()
        self.assertEqual(self.service.query(date.today().year, 11)["status"], "config_error")
        self.client.query.assert_not_called()

    def test_submission_rechecks_records_and_is_cached_once(self):
        draft_id, item = self.draft()
        first = self.service.submit(draft_id, [item])
        self.assertEqual(first["status"], "success")
        self.assertEqual(self.service.submit(draft_id, [item]), first)
        self.client.submit.assert_called_once()
        self.assertEqual(self.client.query.call_count, 2)
        self.assertEqual(self.client.submit.call_args.args[4], [ROW])

    def test_ambiguous_or_changed_record_never_submits(self):
        for rows in [[dict(ROW, qjxs="0.5")], [ROW, ROW], []]:
            draft_id, item = self.draft()
            self.client.query.side_effect = None
            self.client.query.return_value = {"records": rows, "choices": CHOICES}
            self.assertEqual(self.service.submit(draft_id, [item])["status"], "stale")
            self.client.query.side_effect = lambda *args: deepcopy({"records": [ROW], "choices": CHOICES})
        self.client.submit.assert_not_called()

    def test_duplicate_foreign_and_forged_fields_never_submit(self):
        draft_id, item = self.draft()
        for items in [[item, item], [dict(item, record_id="other")], [dict(item, qjt=0)], []]:
            self.assertEqual(self.service.submit(draft_id, items)["status"], "input_error")
        self.client.submit.assert_not_called()

    def test_read_401_recovers_but_submission_is_not_replayed(self):
        draft_id, item = self.draft()
        self.client.query.side_effect = [OaApiError(401), deepcopy({"records": [ROW], "choices": CHOICES})]
        self.client.submit.side_effect = OaApiError(401)
        result = self.service.submit(draft_id, [item])
        self.assertEqual(result["status"], "unknown")
        self.renew.assert_called_once()
        self.assertEqual(self.service.submit(draft_id, [item]), result)
        self.client.submit.assert_called_once()

    def test_timeout_is_unknown_never_success_and_never_retried(self):
        draft_id, item = self.draft()
        self.client.submit.side_effect = TimeoutError("Cookie=private")
        result = self.service.submit(draft_id, [item])
        self.assertEqual(result["status"], "unknown")
        self.assertNotIn("private", json.dumps(result))
        self.service.submit(draft_id, [item])
        self.client.submit.assert_called_once()
        self.renew.assert_not_called()

    def test_proof_required_is_manual_before_writes(self):
        draft_id, item = self.draft()
        self.assertEqual(self.service.submit(draft_id, [dict(item, offset_type="婚假")])["status"], "manual_required")
        self.client.submit.assert_not_called()

    def test_changed_dictionary_expiry_and_database_failure(self):
        draft_id, item = self.draft()
        self.client.query.side_effect = lambda *args: {"records": [ROW], "choices": []}
        self.assertEqual(self.service.submit(draft_id, [item])["status"], "input_error")
        self.service.drafts[draft_id].created -= 601
        self.assertEqual(self.service.submit(draft_id, [item])["status"], "expired")
        self.repository.load = Mock(side_effect=SessionRepositoryError("private"))
        result = self.service.query(date.today().year, 11)
        self.assertEqual(result["status"], "error")
        self.assertNotIn("private", result["message"])
        self.client.submit.assert_not_called()

    def test_worker_executes_with_payload_on_browser_thread(self):
        session = LoginSession(self.config, repository=self.repository)
        self.addCleanup(session.close)
        threads = []

        def query(*args):
            threads.append(threading.get_ident())
            return deepcopy({"records": [ROW], "choices": CHOICES})

        session.attendance.client.query = query
        result = session.call("attendance_query", {"year": date.today().year, "month": 11})
        self.assertEqual(result["status"], "success")
        self.assertNotEqual(threads[0], threading.get_ident())
        session.call("clear")
        self.assertEqual(session.attendance.drafts, {})


class AttendanceSchemaTests(unittest.TestCase):
    def test_time_and_device_requirements_and_forbidden_fields(self):
        base = {"record_id": "record", "offset_type": "忘打卡"}
        for item in [base, dict(base, actual_start="09:00", actual_end="08:00"),
                     dict(base, actual_start="09:00", actual_end="18:00", token="secret"),
                     dict(base, offset_type="卡机故障", actual_start="09:00", actual_end="18:00")]:
            with self.assertRaises(ValidationError):
                AttendanceCorrection.model_validate(item)
        valid = AttendanceCorrection(**base, actual_start="09:00", actual_end="18:00")
        self.assertEqual(valid.actual_start, "09:00:00")

    def test_tool_schema_exposes_no_identity_or_credentials(self):
        session = Mock()
        query, submit = make_attendance_tools(session)
        self.assertEqual(set(query.args_schema.model_json_schema()["properties"]), {"year", "month"})
        session.call.return_value = {"status": "success"}
        submit.invoke({"draft_id": "draft", "items": [{"record_id": "record", "offset_type": "公出"}]})
        self.assertEqual(session.call.call_args.args[0], "attendance_submit")
        self.assertEqual(session.call.call_args.args[1]["items"][0]["record_id"], "record")

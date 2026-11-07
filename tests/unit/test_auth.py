import json
import time
import unittest
from unittest.mock import Mock

from oa_agent.config import LoginConfig
from oa_agent.schemas.auth import Credentials
from oa_agent.services.auth import find_tokens
from oa_agent.sessions.session import LoginSession
from oa_agent.sessions.repository import InMemorySessionRepository


def fake_context(url="https://soa.com.cn/home", password=False, selector=True):
    frame = Mock(url=url)
    frame.evaluate.return_value = {"local": {"auth": json.dumps({"access_token": "secret-token", "password": "private"})}}
    frame.locator.return_value.count.return_value = int(password)
    page = Mock(url=url, frames=[frame])
    page.locator.return_value.count.return_value = int(password)
    page.locator.return_value.first.is_visible.return_value = selector
    context = Mock(pages=[page])
    context.cookies.return_value = [
        {"name": "session", "value": "secret-cookie", "domain": "soa.com.cn", "path": "/oa", "httpOnly": True},
        {"name": "other", "value": "unrelated", "domain": "other.example", "path": "/"},
    ]
    return context


class LoginTests(unittest.TestCase):
    def session(self, **kwargs):
        kwargs.setdefault("username", "example")
        kwargs.setdefault("password", "dummy-password")
        session = LoginSession(LoginConfig(**kwargs), repository=InMemorySessionRepository())
        self.addCleanup(session.close)
        return session

    def test_domain_boundaries(self):
        config = LoginConfig()
        for url in ["https://soa.com.cn.evil.test/", "http://soa.com.cn/", "https://sso.soa.com.cn/"]:
            self.assertFalse(config.accepts_url(url))
        self.assertTrue(config.accepts_url("https://soa.com.cn/home"))
        self.assertFalse(config.accepts_cookie({"domain": "evilsoa.com.cn"}))

    def test_nested_token_without_password(self):
        data = {"auth": json.dumps({"accessToken": "token", "password": "password", "user": "name"})}
        self.assertEqual(find_tokens(data, ("access_token",)), {"auth.accessToken": "token"})
        recursive = {}
        recursive["self"] = recursive
        self.assertEqual(find_tokens(recursive, ("token",)), {})

    def test_collect_retains_scope_but_status_never_secrets(self):
        session = self.session(success_selector="#logout")
        session.browser.context = fake_context()
        result = session.auth.collect()
        self.assertEqual(result["state"], "verified")
        self.assertEqual(result["cookie_count"], 1)
        self.assertEqual(result["token_count"], 1)
        self.assertNotIn("secret", json.dumps(result))
        self.assertEqual(session.store.credentials.cookies[0]["path"], "/oa")
        self.assertTrue(session.store.credentials.cookies[0]["httpOnly"])

    def test_failed_rules_clear_previous_snapshot(self):
        session = self.session(success_selector="#logout", success_url_regex=r"/home$")
        session.store.credentials = Credentials(tokens={"token": "old"})
        session.browser.context = fake_context(selector=False)
        self.assertEqual(session.auth.collect()["token_count"], 0)
        self.assertFalse(session.store.credentials.verified)
        session.browser.context = fake_context()
        self.assertEqual(session.auth.collect()["state"], "verified")
        session.browser.context = fake_context(url="https://soa.com.cn/login")
        self.assertEqual(session.auth.collect()["state"], "waiting_login")

    def test_password_page_not_collected(self):
        session = self.session()
        session.browser.context = fake_context(password=True)
        self.assertEqual(session.auth.collect()["cookie_count"], 0)

    def test_actual_oa_token_cookie_is_decoded_without_changing_cookie(self):
        session = self.session()
        session.browser.context = fake_context(url="https://sio.soa.com.cn/")
        session.browser.context.cookies.return_value = [
            {"name": "DP_Token", "value": "abc%2Fdef%3D", "domain": ".soa.com.cn", "path": "/"}
        ]
        session.auth._authenticated_token = "abc/def="
        session.auth.collect()
        self.assertEqual(session.store.credentials.tokens["cookie:.soa.com.cn:/:DP_Token"], "abc/def=")
        self.assertEqual(session.store.credentials.cookies[0]["value"], "abc%2Fdef%3D")

    def test_no_session_or_untrusted_page(self):
        session = self.session()
        self.assertEqual(session.auth.collect()["state"], "closed")
        session.browser.context = fake_context(url="https://untrusted.test/")
        self.assertEqual(session.auth.collect()["cookie_count"], 0)

    def test_business_api_token_uses_verified_source_and_never_other_auth_headers(self):
        session = self.session()
        session.store.credentials = Credentials(tokens={"header:token": "candidate"})
        self.assertEqual(session.auth.api_token(), "")
        session.store.credentials.verified = True
        session.store.credentials.tokens["cookie:.soa.com.cn:/:DP_Token"] = "decoded-token"
        self.assertEqual(session.auth.api_token(), "decoded-token")
        session.auth._authenticated_token = "validated-token"
        session.store.credentials.tokens["header:token"] = "validated-token"
        self.assertEqual(session.auth.api_token(), "validated-token")
        session.auth.clear()
        self.assertEqual(session.auth.api_token(), "")

    def test_cookie_alone_does_not_prove_login(self):
        session = self.session()
        session.browser.context = fake_context()
        session.browser.context.cookies.return_value = [
            {"name": "DP_Token", "value": "candidate", "domain": "soa.com.cn", "path": "/"}
        ]
        self.assertEqual(session.auth.collect()["state"], "waiting_login")

    def test_identity_response_must_match_current_cookie(self):
        session = self.session()
        session.browser.context = fake_context()
        session.browser.context.cookies.return_value = [
            {"name": "DP_Token", "value": "current", "domain": "soa.com.cn", "path": "/"}
        ]
        response = Mock(url=session.config.user_info_url, status=200, headers={})
        response.json.return_value = {"code": 200, "data": {"uname": "example"}}
        response.request.all_headers.return_value = {"token": "stale"}
        session.auth.on_response(response)
        self.assertEqual(session.auth.collect()["state"], "waiting_login")
        response.request.all_headers.return_value = {"token": "current"}
        session.auth.on_response(response)
        self.assertEqual(session.auth.collect()["state"], "verified")
        response.status = 401
        session.auth.on_response(response)
        self.assertEqual(session.store.credentials.tokens, {})
        self.assertEqual(session.store.state, "waiting_login")

    def test_failed_user_info_response_is_not_success(self):
        session = self.session()
        for body in ({"code": 401, "USRID": "example"}, {"code": 200, "data": {}}, {}):
            response = Mock(url=session.config.user_info_url, status=200, headers={})
            response.json.return_value = body
            response.request.all_headers.return_value = {"token": "candidate"}
            session.auth.on_response(response)
            self.assertEqual(session.auth._authenticated_token, "")

    def test_worker_automatically_collects_without_collect_command(self):
        session = self.session(success_selector="#logout")
        session.browser.context = fake_context()
        self.assertEqual(session.call("status")["state"], "verified")
        self.assertEqual(session.credentials().cookies[0]["value"], "secret-cookie")

    def test_headers_filtered_and_clear_isolated(self):
        first, second = self.session(), self.session()
        first.auth.on_request(Mock(url="https://untrusted.test/", all_headers=Mock(return_value={"Authorization": "secret"})))
        self.assertEqual(first.auth._headers, {})
        first.auth.on_request(Mock(url="https://soa.com.cn/api", all_headers=Mock(return_value={"Authorization": "Bearer secret", "password": "hidden"})))
        self.assertEqual(first.auth._headers, {"header:authorization": "Bearer secret"})
        self.assertEqual(second.auth._headers, {})
        first._clear()
        self.assertEqual(first.auth._headers, {})
        self.assertEqual(first.auth._authenticated_token, "")

    def test_worker_without_browser_and_copy_isolation(self):
        session = self.session()
        self.assertEqual(session.call("status")["state"], "closed")
        snapshot = session.credentials()
        snapshot.tokens["token"] = "external"
        self.assertEqual(session.credentials().tokens, {})
        self.assertEqual(session.call("clear")["cookie_count"], 0)

    def test_idle_expiration_clears_credentials_even_if_status_is_polled(self):
        session = self.session(idle_seconds=1)
        session.browser.context = fake_context()
        session.store.credentials = Credentials(tokens={"token": "secret"})
        session.call("status")
        time.sleep(1.2)
        result = session.call("status")
        self.assertEqual(result["state"], "expired")
        self.assertEqual(result["token_count"], 0)


if __name__ == "__main__":
    unittest.main()

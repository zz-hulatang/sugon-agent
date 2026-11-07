"""真实 Chrome 验证自动填表、滑动/按钮提交、MFA 和失败后不重试；全部网络由模拟响应接管。"""
import json
from pathlib import Path
import time
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from oa_agent.config import LoginConfig
from oa_agent.sessions.session import LoginSession
from oa_agent.sessions.repository import InMemorySessionRepository


original_factory = sync_playwright
login_html = (Path(__file__).resolve().parents[1] / "fixtures" / "password_login.html").read_text()
home_html = '''<!doctype html><h1>模拟 OA 首页</h1><script>
fetch('https://msp.soa.com.cn/api/sap/getLoginUser', {headers: {token: 'auto/token'}});
</script>'''


def run_scenario(mode, repository=None):
    attempts = []

    def factory():
        manager = original_factory()
        original_start = manager.start

        def start():
            pw = original_start()
            original_launch = pw.chromium.launch

            def launch(**kwargs):
                kwargs["headless"] = True
                browser = original_launch(**kwargs)
                original_context = browser.new_context

                def new_context(*args, **kwargs):
                    context = original_context(*args, **kwargs)

                    def handle(route):
                        url = route.request.url
                        if url == "https://soa.com.cn/sioLogin/":
                            route.fulfill(status=200, content_type="text/html", body=login_html.replace("__MODE__", mode))
                        elif url == "https://soa.com.cn/test-auth":
                            # 只接收测试凭据；失败时也只输出固定断言，避免打印认证数据。
                            data = route.request.post_data_json
                            assert data == {"username": "dummy-user", "password": "dummy-password"}, "模拟登录参数错误"
                            attempts.append(True)
                            route.fulfill(status=200, content_type="application/json", body=json.dumps({"mode": mode}))
                        elif url == "https://soa.com.cn/home":
                            route.fulfill(status=200, content_type="text/html", body=home_html,
                                          headers={"Set-Cookie": "SESSION=auto-session; Secure; HttpOnly; Path=/"})
                        elif url == "https://msp.soa.com.cn/api/sap/getLoginUser":
                            route.fulfill(status=200, content_type="application/json",
                                          body=json.dumps({"code": 200, "data": {"uname": "dummy-user"}}),
                                          headers={"Access-Control-Allow-Origin": "https://soa.com.cn",
                                                   "Access-Control-Allow-Headers": "token"})
                        else:
                            route.abort()

                    context.route("**/*", handle)
                    return context

                browser.new_context = new_context
                return browser

            pw.chromium.launch = launch
            return pw

        manager.start = start
        return manager

    config = LoginConfig(username="dummy-user", password="dummy-password", browser_channel="chrome",
                         submit_selector="#submit" if mode == "button" else "#login-slider")
    with patch("playwright.sync_api.sync_playwright", factory):
        session = LoginSession(config, repository=repository if repository is not None else InMemorySessionRepository())
        try:
            session.call("open")
            expected = "needs_verification" if mode == "mfa" else "failed" if mode == "failure" else "completed"
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                result = session.call("status")
                assert "dummy-password" not in json.dumps(result)
                if result.get("auto_login") == expected:
                    break
                time.sleep(0.1)
            assert result.get("auto_login") == expected, (mode, result)
            if mode in {"slider", "button"}:
                assert result["state"] == "verified"
                credentials = session.credentials()
                assert credentials.tokens["cookie:soa.com.cn:/:DP_Token"] == "auto/token"
                assert any(cookie["httpOnly"] for cookie in credentials.cookies)
                assert session.repository.load("dummy-user").token == "auto/token"
            else:
                assert result["state"] == "waiting_login"
                assert not session.credentials().tokens
                time.sleep(1.5)
            assert len(attempts) == 1, "同一会话不应重复提交账号密码"
            session.call("clear")
            assert session.credentials().tokens == {}
            print(f"{mode}: 通过")
        finally:
            session.close()


if __name__ == "__main__":
    for scenario in ("slider", "button", "mfa", "failure"):
        run_scenario(scenario)

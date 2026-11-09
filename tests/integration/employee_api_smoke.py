"""使用回环 HTTP 服务验证真实 Playwright 请求、Cookie、401 与重定向；数据全部虚构。"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

from playwright.sync_api import sync_playwright

from oa_agent.clients.oa_client import OaApiError, OaClient
from oa_agent.clients.browser import BrowserClient
from oa_agent.config import LoginConfig
from oa_agent.schemas.auth import Credentials
from oa_agent.services.employee import EmployeeService
from oa_agent.sessions.store import CredentialStore
from oa_agent.sessions.repository import InMemorySessionRepository
from oa_agent.services.authenticated_request import AuthenticatedRequestService


def main():
    profile = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "employee_detail.json").read_text())
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            # 收集的认证数据均为本测试创建的虚构值，不输出到日志。
            seen.append((self.path, self.headers.get("token"), self.headers.get("Cookie", "")))
            status = 200 if len(seen) in {1, 3, 5} else 401 if len(seen) == 2 else 302
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Location", "/unexpected-redirect")
            self.end_headers()
            data = dict(profile, uname="other.user") if self.path.endswith("/other.user") else profile
            self.wfile.write(json.dumps(data if status == 200 else {}).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            request = playwright.request.new_context(storage_state={"cookies": [
                {"name": "SESSION", "value": "dummy-session", "domain": "127.0.0.1",
                 "path": "/api/", "expires": -1, "httpOnly": True, "secure": False, "sameSite": "Lax"},
                {"name": "OTHER", "value": "not-for-this-path", "domain": "127.0.0.1",
                 "path": "/other/", "expires": -1, "httpOnly": True, "secure": False, "sameSite": "Lax"},
            ], "origins": []})
            try:
                browser = BrowserClient(LoginConfig())
                browser._playwright = playwright
                browser.context = SimpleNamespace(request=request)
                client = OaClient(browser)
                repository = InMemorySessionRepository()
                cookies = request.storage_state()["cookies"]
                repository.save("demo.user", Credentials(cookies=cookies, verified=True), "dummy-token")
                fresh_cookies = [dict(cookie, value="fresh-session") if cookie["name"] == "SESSION" else cookie for cookie in cookies]
                renew = Mock(side_effect=lambda: repository.save("demo.user", Credentials(cookies=fresh_cookies, verified=True), "fresh-token"))
                requests = AuthenticatedRequestService("demo.user", repository, renew, Mock())
                service = EmployeeService(LoginConfig(username="demo.user", password="dummy-password"), client, requests)
                # 只在测试中替换为回环地址，生产代码固定请求用户提供的 HTTPS 接口。
                base = f"http://127.0.0.1:{server.server_port}/api/sap/getEmployeeDetail/"
                with patch.object(OaClient, "EMPLOYEE_DETAIL_BASE_URL", base):
                    assert service.get_current_user()["data"] == profile
                    assert seen[0][0] == "/api/sap/getEmployeeDetail/demo.user"
                    assert seen[0][1] == "dummy-token"
                    assert "SESSION=dummy-session" in seen[0][2]
                    assert "OTHER=" not in seen[0][2]
                    result = service.get_current_user()
                    assert result["status"] == "success"
                    renew.assert_called_once()
                    assert seen[2][1] == "fresh-token" and "SESSION=fresh-session" in seen[2][2]
                    try:
                        client.get_employee_detail("demo.user", "dummy-token", cookies=cookies)
                    except OaApiError as error:
                        assert error.status_code == 302
                    else:
                        raise AssertionError("重定向不应被自动跟随")
                    assert len(seen) == 4
                    result = service.get_user_info("other.user")
                    assert result["data"]["uname"] == "other.user"
                    assert seen[4][0] == "/api/sap/getEmployeeDetail/other.user"
                    assert seen[4][1] == "fresh-token" and "SESSION=fresh-session" in seen[4][2]
                    assert repository.load("other.user") is None
                print("真实 Playwright API 检查通过：默认及指定账号查询复用本机会话，401 更新后原调用自动成功。")
            finally:
                request.dispose()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    main()

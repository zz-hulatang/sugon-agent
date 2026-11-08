"""真实 Chrome 运行提交桥接脚本；路由全部拦截，无真实 OA 网络请求或工单。"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import date
import json
from pathlib import Path
from urllib.parse import urlsplit

from oa_agent.clients.attendance_client import AttendanceClient, FORM_URL
from oa_agent.clients.browser import BrowserClient
from oa_agent.config import LoginConfig
from oa_agent.schemas.auth import Credentials
from oa_agent.services.attendance import AttendanceService
from oa_agent.services.authenticated_request import AuthenticatedRequestService
from oa_agent.sessions.repository import InMemorySessionRepository


ROW = {"rq": f"{date.today().year}-11-02", "dk": "09:05:15", "dk2": "18:26:32",
       "lx": "旷工", "qjxs": "0.08", "qjt": "0.01", "workday": True}
CHOICES = [{"type": "公出", "typecode": "A008"}]


def main():
    html = (Path(__file__).resolve().parents[1] / "fixtures" / "attendance.html").read_text()
    config = LoginConfig(username="demo.user", password="test-only")
    browser = BrowserClient(config)
    business_page = browser.business_page
    writes = []
    scenario = {"mode": "success", "state": "ok"}

    def route_handler(route):
        path = urlsplit(route.request.url).path
        if route.request.url == FORM_URL:
            route.fulfill(status=200, content_type="text/html", body=html)
        elif route.request.method == "POST" and path in {
                "/gateway/attendance/leaveapp/updateLeaveApply/leader", "/appForm/FormSubmit", "/FlowEngine/setNodeMsg"}:
            writes.append((path, route.request.post_data))
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"STATE": scenario["state"]}))
        else:
            route.abort()  # 绝不回退到真实网络。

    @contextmanager
    def mock_page(cookies):
        with business_page(cookies) as page:
            page.route("**/*", route_handler)
            page.add_init_script(f"document.addEventListener('DOMContentLoaded', () => window.scenario = {json.dumps(scenario['mode'])});")
            yield page

    browser.business_page = mock_page
    client = AttendanceClient(browser)
    client.query = lambda *args: deepcopy({"records": [ROW], "choices": CHOICES})
    repository = InMemorySessionRepository()
    repository.save("demo.user", Credentials(verified=True), "fake-token")
    requests = AuthenticatedRequestService("demo.user", repository, lambda: None, lambda: None)
    service = AttendanceService(config, client, requests)

    def submit():
        result = service.query(date.today().year, 11)
        draft = result["draft_id"]
        items = [{"record_id": result["records"][0]["record_id"], "offset_type": "公出", "remark": "测试备注'\"<>&"}]
        submitted = service.submit(draft, items)
        assert service.submit(draft, items) == submitted  # 再次调用不能重复写入。
        return submitted

    try:
        result = submit()
        assert result["status"] == "success", result
        assert len(writes) == 3, writes
        detail = json.loads(writes[0][1])
        assert len(detail) == 1 and detail[0]["rq"] == ROW["rq"]
        assert detail[0]["memo"] == "测试备注'\"<>&" and detail[0]["dxlbbm"] == "A008"
        assert detail[0]["qjt"] == ROW["qjt"]  # 时长保留服务端原值。

        scenario["mode"] = "validation"
        assert submit()["status"] == "manual_required"
        assert len(writes) == 3  # 原页面校验失败不能触发最终写请求。

        scenario["mode"] = "multiple"
        assert submit()["status"] == "manual_required"
        assert len(writes) == 3  # 多分支不自动选路由。

        scenario.update(mode="success", state="error")
        assert submit()["status"] == "unknown"
        assert len(writes) == 6  # 业务失败也不因重复调用重发。
        print("异常考勤模拟链路通过：真实 Chrome 执行桥接脚本，备注转义、选中记录、动态字段、流程成功判定、校验阻断和重复调用均符合预期。")
    finally:
        browser.close()


if __name__ == "__main__":
    main()

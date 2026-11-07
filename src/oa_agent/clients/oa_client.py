"""在会话工作线程中调用 OA API，复用浏览器 Cookie 的域名和路径约束。"""
from urllib.parse import quote

from oa_agent.clients.browser import BrowserClient


class OaApiError(Exception):
    """仅携带 HTTP 状态码，不将响应原文或认证头带入错误消息。"""

    def __init__(self, status_code: int | None = None):
        super().__init__("OA 接口调用失败")
        self.status_code = status_code


class OaClient:
    EMPLOYEE_DETAIL_BASE_URL = "https://msp.soa.com.cn/api/sap/getEmployeeDetail/"

    def __init__(self, browser: BrowserClient):
        self.browser = browser

    def get_employee_detail(self, username: str, token: str, cookies: list[dict]) -> dict:
        """只使用仓库认证快照，不回退到浏览器内存 Cookie。"""
        with self.browser.request_context(cookies) as request:
            return self._get_employee_detail(request, username, token)

    def _get_employee_detail(self, request, username: str, token: str) -> dict:
        """获取配置账号的员工详情；禁止重定向，避免认证头被带往其他地址。"""
        if not username.strip() or username in {".", ".."}:
            raise OaApiError()
        response = None
        try:
            # 用户名只占一个 URL 路径段，不能将其中的 /、? 等当成接口路径或查询参数。
            url = self.EMPLOYEE_DETAIL_BASE_URL + quote(username, safe="")
            response = request.get(
                url,
                headers={"token": token} if token else {},
                timeout=15000,
                max_redirects=0,
                fail_on_status_code=False,
            )
            if response.status != 200:
                raise OaApiError(response.status)
            data = response.json()
            # 约定成功响应为员工对象，不把 HTML 登录页或无效 JSON 当成员工信息。
            if not isinstance(data, dict) or not isinstance(data.get("uname"), str) or not data["uname"].strip():
                raise OaApiError(200)
            return data
        except OaApiError:
            raise
        except Exception:
            # Playwright 异常可能包含请求头、URL 或响应内容，只保留固定错误。
            raise OaApiError() from None
        finally:
            if response is not None:
                # APIRequestContext 会缓存响应，及时释放包含个人信息的响应体。
                try:
                    response.dispose()
                except Exception:
                    pass

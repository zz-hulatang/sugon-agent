"""员工信息业务逻辑：默认或指定查询账号、认证前置检查、接口错误转换。"""
from oa_agent.clients.oa_client import OaApiError, OaClient
from oa_agent.config import LoginConfig
from oa_agent.schemas.employee import EmployeeResult
from oa_agent.services.authenticated_request import AuthenticatedRequestService, LoginRecoveryError
from oa_agent.sessions.repository import SessionRepositoryError


class EmployeeService:
    def __init__(self, config: LoginConfig, client: OaClient, requests: AuthenticatedRequestService):
        self.config = config
        self.client = client
        self.requests = requests

    def get_current_user(self) -> EmployeeResult:
        """保留查询本机配置账号的后端入口。"""
        return self.get_user_info()

    def get_user_info(self, username: str | None = None) -> EmployeeResult:
        """查询账号可以指定，认证和登录恢复始终使用本机配置账号。"""
        configured_username = self.config.username.strip()
        if not configured_username:
            return {"status": "config_error", "message": "请先在本机配置 OA_USERNAME，并重启应用。"}
        if username is not None and (not isinstance(username, str) or not username.strip()):
            return {"status": "invalid_request", "message": "查询其他账号时，请提供非空的 OA 账号名称。"}
        username = configured_username if username is None else username.strip()
        if username in {".", ".."}:
            return {"status": "invalid_request", "message": "请提供有效的 OA 账号名称。"}
        try:
            data = self.requests.execute(lambda saved: self.client.get_employee_detail(
                username, saved.token, cookies=saved.credentials.cookies))
            if data["uname"].casefold() != username.casefold():
                return {"status": "error", "message": "接口返回的账号与查询账号不一致，请检查账号名称或接口返回。"}
            message = ("已获取当前用户信息。" if username.casefold() == configured_username.casefold()
                       else "已获取指定账号的用户信息。")
            return {"status": "success", "message": message, "data": data}
        except OaApiError as error:
            if error.status_code == 401:
                return {"status": "auth_required", "status_code": 401,
                        "message": "重新登录后接口仍返回 401，已清除失效会话，请检查账号权限。"}
            result: EmployeeResult = {"status": "error", "message": "获取用户信息失败，请检查 OA 网络或接口返回。"}
            if error.status_code is not None:
                result["status_code"] = error.status_code
            return result
        except SessionRepositoryError as error:
            return {"status": "error", "message": error.public_message}
        except LoginRecoveryError:
            return {"status": "auth_required", "message": "自动恢复登录未完成或已取消，请检查浏览器验证及登录配置。"}

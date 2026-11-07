"""将当前用户员工详情查询注册为 LangChain 工具。"""
from langchain.tools import tool

from oa_agent.sessions.session import LoginSession


def make_employee_tool(session: LoginSession):
    @tool
    def get_user_info() -> dict:
        """获取当前配置 OA_USERNAME 对应的员工详细信息。

        可查询姓名、工号、部门、岗位、联系方式、入职日期等接口返回字段。
        账号由本机配置提供，Cookie 和 Token 每次从 MySQL 读取，无需传入凭据。
        缺少会话或返回 401 时，后端自动登录并继续原查询，无需用户再次输入任务。
        若需短信或 MFA，用户在浏览器完成，本次调用将等待恢复结果。
        auth_required 代表恢复失败、被取消或恢复后仍无权限，不应无限重试。
        """
        try:
            return session.call("user_info")
        except Exception:
            return {"status": "error", "message": "获取用户信息失败，请检查 OA 会话和网络。"}

    return get_user_info

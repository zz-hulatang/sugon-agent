"""将默认本机账号、可指定其他账号的员工详情查询注册为 LangChain 工具。"""
from langchain.tools import tool

from oa_agent.sessions.session import LoginSession


def make_employee_tool(session: LoginSession):
    @tool
    def get_user_info(username: str | None = None) -> dict:
        """查询员工详细信息，默认查询本机配置 OA_USERNAME 对应的账号。

        可查询姓名、工号、部门、岗位、联系方式、入职日期等接口返回字段。
        username 为可选的查询账号名称；省略或为 null 时查询本机配置账号。
        查询其他账号时，必须将用户提供的 OA 账号名称传入 username，不能猜测或用姓名替代。
        用户要查询他人但未提供账号名称时，先请用户补充，不要退回查询本机账号。
        显式提供的 username 不能为空或仅含空白。
        认证始终使用本机账号的会话，Cookie 和 Token 每次从 MySQL 读取，无需传入凭据。
        缺少会话或返回 401 时，后端自动登录并继续原查询，无需用户再次输入任务。
        若需短信或 MFA，用户在浏览器完成，本次调用将等待恢复结果。
        auth_required 代表恢复失败、被取消或恢复后仍无权限，不应无限重试。
        """
        try:
            if username is None:
                return session.call("user_info")
            return session.call("user_info", {"username": username})
        except Exception:
            return {"status": "error", "message": "获取用户信息失败，请检查 OA 会话和网络。"}

    return get_user_info

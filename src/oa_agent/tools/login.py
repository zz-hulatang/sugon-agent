"""将当前用户的登录会话适配为 LangChain 工具，认证逻辑由服务层处理。"""
from typing import Literal
from langchain.tools import tool
from oa_agent.sessions.session import LoginSession


def make_login_tool(session: LoginSession):
    """通过闭包绑定当前用户的会话，避免模型传入其他用户的会话标识。"""

    # Literal 同时约束工具参数：模型只可打开页面或读状态，不能取得原始凭据。
    @tool
    def oa_login(action: Literal["open", "status"] = "open") -> dict:
        """打开 OA 登录页面，或查询本地会话状态。

        open 打开 https://soa.com.cn/，不等待用户输入密码。
        后端已配置本机账号密码时自动填写和提交，否则由用户在浏览器手动登录。
        若需验证码或 MFA，用户在浏览器完成；成功后自动采集会话，无需额外点击。
        status 只返回状态与凭据数量，不返回 Cookie 或 Token 原文。
        """
        try:
            return session.call(action)
        except Exception:
            # 工具结果会进入模型上下文，不能返回可能包含 SSO 凭据的原始异常。
            return {"state": "error", "message": "OA 操作失败，请检查本机网络和浏览器安装。"}

    return oa_login

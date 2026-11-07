"""认证相关数据结构；凭据关闭自动 repr，避免日志打印时泄漏原文。"""
from dataclasses import dataclass, field
from typing import Literal, NotRequired, TypedDict

SessionState = Literal["closed", "waiting_login", "verified", "expired", "error"]


class SessionStatus(TypedDict):
    """界面和 Agent 可见的状态，不包含 Cookie 或 Token 原文。"""

    state: SessionState
    cookie_count: int
    token_count: int
    verified: bool
    message: str
    auto_login: NotRequired[str]
    recovering: NotRequired[bool]
    persistence_error: NotRequired[bool]


@dataclass(repr=False)
class Credentials:
    """仅供后端使用的凭据快照；verified 表示通过登录规则，不保证永久有效。"""

    # 保留 Cookie 的 domain/path/secure/httpOnly 等属性，后续请求需遵守其作用域。
    cookies: list[dict] = field(default_factory=list)
    # 来源路径 -> Token 原值；不同来源可能共存，不能任取一项作为所有接口的认证头。
    tokens: dict[str, str] = field(default_factory=dict)
    captured_at: float = 0
    verified: bool = False

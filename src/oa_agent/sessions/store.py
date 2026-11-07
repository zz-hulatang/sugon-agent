"""保存单个用户的内存状态；生产调用由所属会话线程串行读写。"""
from copy import deepcopy
from dataclasses import dataclass, field

from oa_agent.schemas.auth import Credentials, SessionState, SessionStatus


@dataclass(repr=False)
class CredentialStore:
    """凭据与对外状态分开管理，避免把原始认证数据放入工具返回值。"""

    credentials: Credentials = field(default_factory=Credentials)
    state: SessionState = "closed"

    def snapshot(self) -> Credentials:
        """深拷贝给可信后端，避免调用者修改会话原值；副本由调用者负责清理。"""
        return deepcopy(self.credentials)

    def reset(self, state: SessionState = "closed") -> None:
        """丢弃当前快照并切换状态，不向磁盘持久化认证信息。"""
        self.credentials = Credentials()
        self.state = state

    def status(self, message: str) -> SessionStatus:
        """只返回状态、凭据数量和固定提示，可供界面与模型读取。"""
        return {
            "state": self.state,
            "cookie_count": len(self.credentials.cookies),
            "token_count": len(self.credentials.tokens),
            "verified": self.credentials.verified,
            "message": message,
        }

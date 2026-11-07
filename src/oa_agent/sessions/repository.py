"""会话持久化接口；内存实现只用于隔离测试，应用使用 MySQL 实现。"""
from copy import deepcopy
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from oa_agent.schemas.auth import Credentials


class SessionRepositoryError(Exception):
    """内部异常与可公开诊断分离，只公开预定义提示，避免带出连接凭据。"""

    def __init__(self, message="认证会话仓库操作失败", *, code="repository_error"):
        super().__init__(message)
        self.code = code

    @property
    def public_message(self):
        messages = {
            "missing_config": "请在本机配置 MYSQL_USER 和 SESSION_ENCRYPTION_KEY，并检查 MYSQL_PASSWORD。",
            "database_not_found": "MYSQL_DATABASE 指定的数据库不存在，请先创建数据库。",
            "access_denied": "MySQL 拒绝连接，请检查数据库账号、密码和允许连接的来源。",
            "permission_denied": "MySQL 权限不足，请检查数据库和会话表的访问权限。",
            "connection_failed": "无法连接 MySQL，请检查服务是否启动、主机和端口是否正确。",
            "connection_lost": "MySQL 连接中断，请检查数据库服务和网络后重试。",
            "schema_mismatch": "MySQL 会话表结构不匹配，请检查 sql/001_auth_sessions.sql。",
            "decrypt_failed": "认证会话无法解密，请检查 SESSION_ENCRYPTION_KEY 是否与保存时一致。",
        }
        return messages.get(self.code, "无法读取或更新 MySQL 认证会话，请检查数据库配置、连接和加密密钥。")


@dataclass(repr=False)
class SavedSession:
    username: str
    credentials: Credentials
    token: str
    revision: str


class SessionRepository(Protocol):
    def save(self, username: str, credentials: Credentials, token: str) -> SavedSession: ...
    def load(self, username: str) -> SavedSession | None: ...
    def delete(self, username: str, revision: str | None = None) -> None: ...


class InMemorySessionRepository:
    """测试替身：每个实例独立，生产请求不会自动降级到此实现。"""

    def __init__(self):
        self.rows = {}

    def save(self, username, credentials, token):
        record = SavedSession(username, deepcopy(credentials), token, uuid4().hex)
        self.rows[username] = record
        return deepcopy(record)

    def load(self, username):
        return deepcopy(self.rows.get(username))

    def delete(self, username, revision=None):
        current = self.rows.get(username)
        if current and (revision is None or revision == current.revision):
            self.rows.pop(username)

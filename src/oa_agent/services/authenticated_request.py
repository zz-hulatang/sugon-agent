"""从持久化仓库读取认证，401 后恢复登录并继续原请求，不依赖模型重发任务。"""
from collections.abc import Callable

from oa_agent.clients.oa_client import OaApiError
from oa_agent.sessions.repository import SavedSession, SessionRepository


class LoginRecoveryError(Exception):
    pass


class AuthenticatedRequestService:
    def __init__(self, username: str, repository: SessionRepository,
                 renew_login: Callable[[], None], invalidate_local: Callable[[], None], on_loaded=None):
        self.username = username
        self.repository = repository
        self.renew_login = renew_login
        self.invalidate_local = invalidate_local
        self.on_loaded = on_loaded or (lambda record: None)

    def execute(self, operation: Callable[[SavedSession], dict]) -> dict:
        """当前仅用于可安全重试的用户信息 GET；一次任务最多恢复登录一次。"""
        record = self.repository.load(self.username)
        renewed = record is None
        if renewed:
            self.renew_login()
            record = self.repository.load(self.username)
        if record is None:
            raise LoginRecoveryError("登录完成后未找到数据库会话")
        self.on_loaded(record)
        try:
            return operation(record)
        except OaApiError as error:
            if error.status_code != 401:
                raise
        self.repository.delete(self.username, record.revision)
        self.invalidate_local()
        if renewed:
            raise OaApiError(401)
        # 如果其他会话已经更新数据库，直接使用最新版本，避免重复登录。
        current = self.repository.load(self.username)
        if current is None:
            self.renew_login()
        record = self.repository.load(self.username)
        if record is None:
            raise LoginRecoveryError("重新登录未产生可用的数据库会话")
        self.on_loaded(record)
        try:
            return operation(record)
        except OaApiError as error:
            if error.status_code == 401:
                self.repository.delete(self.username, record.revision)
                self.invalidate_local()
            raise

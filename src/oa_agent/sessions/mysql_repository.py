"""按用户保存最新认证会话；Cookie 和 Token 以 Fernet 密文存入 MySQL。"""
from dataclasses import asdict
import json
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
import pymysql

from oa_agent.config import MySQLConfig
from oa_agent.schemas.auth import Credentials
from oa_agent.sessions.repository import SavedSession, SessionRepositoryError


SCHEMA_SQL = """CREATE TABLE IF NOT EXISTS oa_auth_sessions (
    username VARCHAR(191) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin PRIMARY KEY,
    payload LONGBLOB NOT NULL,
    revision CHAR(32) NOT NULL,
    captured_at DOUBLE NOT NULL,
    updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""


def repository_error(error, fallback):
    """按驱动错误码分类，不公开可能含账号、连接串或认证数据的异常原文。"""
    if isinstance(error, SessionRepositoryError):
        return error
    codes = {1049: "database_not_found", 1045: "access_denied", 1044: "permission_denied",
             1142: "permission_denied", 1143: "permission_denied", 2003: "connection_failed",
             2006: "connection_lost", 2013: "connection_lost", 1054: "schema_mismatch"}
    reason = "repository_error"
    if isinstance(error, pymysql.MySQLError) and error.args:
        reason = codes.get(error.args[0], reason)
    elif isinstance(error, InvalidToken):
        reason = "decrypt_failed"
    return SessionRepositoryError(fallback, code=reason)


class MySQLSessionRepository:
    def __init__(self, config: MySQLConfig):
        self.config = config
        self._schema_ready = False

    def _connect(self):
        if not self.config.configured:
            raise SessionRepositoryError("数据库配置不完整", code="missing_config")
        options = dict(host=self.config.host, port=self.config.port, user=self.config.user,
                       password=self.config.password, database=self.config.database, charset="utf8mb4",
                       connect_timeout=5, read_timeout=10, write_timeout=10, autocommit=True,
                       cursorclass=pymysql.cursors.DictCursor)
        if self.config.ssl_ca:
            options.update(ssl_ca=self.config.ssl_ca, ssl_verify_cert=True, ssl_verify_identity=True)
        connection = pymysql.connect(**options)
        if not self._schema_ready:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(SCHEMA_SQL)
                self._schema_ready = True
            except Exception:
                connection.close()
                raise
        return connection

    def save(self, username: str, credentials: Credentials, token: str) -> SavedSession:
        if not username or not credentials.verified:
            raise SessionRepositoryError("仅保存已验证的用户会话")
        revision = uuid4().hex
        try:
            # 将用户名与版本一同放入密文，读取时能识别密文被移到其他用户行的情况。
            data = {"username": username, "revision": revision, "credentials": asdict(credentials), "token": token}
            payload = Fernet(self.config.encryption_key.encode()).encrypt(json.dumps(data, ensure_ascii=False).encode())
            connection = self._connect()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("""INSERT INTO oa_auth_sessions (username, payload, revision, captured_at)
                        VALUES (%s, %s, %s, %s) ON DUPLICATE KEY UPDATE
                        payload=%s, revision=%s, captured_at=%s, updated_at=CURRENT_TIMESTAMP(6)""",
                        (username, payload, revision, credentials.captured_at, payload, revision, credentials.captured_at))
            finally:
                connection.close()
            return SavedSession(username, credentials, token, revision)
        except Exception as error:
            raise repository_error(error, "会话保存失败") from None

    def load(self, username: str) -> SavedSession | None:
        try:
            connection = self._connect()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT payload, revision FROM oa_auth_sessions WHERE username=%s", (username,))
                    row = cursor.fetchone()
            finally:
                connection.close()
            if not row:
                return None
            data = json.loads(Fernet(self.config.encryption_key.encode()).decrypt(bytes(row["payload"])))
            if data["username"] != username or data["revision"] != row["revision"]:
                raise ValueError("invalid session binding")
            credentials = Credentials(**data["credentials"])
            if not credentials.verified:
                raise ValueError("unverified session")
            return SavedSession(username, credentials, data["token"], row["revision"])
        except Exception as error:
            raise repository_error(error, "会话读取失败") from None

    def delete(self, username: str, revision: str | None = None) -> None:
        try:
            connection = self._connect()
            try:
                with connection.cursor() as cursor:
                    # 防止旧请求的 401 删除另一会话刚写入的新登录结果。
                    if revision is None:
                        cursor.execute("DELETE FROM oa_auth_sessions WHERE username=%s", (username,))
                    else:
                        cursor.execute("DELETE FROM oa_auth_sessions WHERE username=%s AND revision=%s", (username, revision))
            finally:
                connection.close()
        except Exception as error:
            raise repository_error(error, "会话失效标记失败") from None

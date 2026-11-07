"""隔离 MySQL 的真实加密读写与任务恢复；仅使用虚构账号，不读取项目 .env。"""
import os
import time
from unittest.mock import Mock

from cryptography.fernet import Fernet
import pymysql

from oa_agent.config import MySQLConfig
from oa_agent.schemas.auth import Credentials
from oa_agent.clients.oa_client import OaApiError
from oa_agent.sessions.mysql_repository import MySQLSessionRepository
from oa_agent.services.authenticated_request import AuthenticatedRequestService


def main():
    config = MySQLConfig(host="127.0.0.1", port=int(os.environ["MYSQL_SMOKE_PORT"]), database="oa_agent",
                         user="oa_test", password="local-test-only", encryption_key=Fernet.generate_key().decode())
    repository = MySQLSessionRepository(config)
    deadline = time.monotonic() + 90
    while True:
        try:
            repository.load("test-user")
            break
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(1)
    credentials = Credentials(cookies=[{"name": "SESSION", "value": "fake-cookie"}],
                              tokens={"header:token": "old-token"}, captured_at=time.time(), verified=True)
    old = repository.save("test-user", credentials, "old-token")
    self_check = repository.load("test-user")
    assert self_check.token == "old-token" and self_check.credentials.cookies == credentials.cookies
    connection = pymysql.connect(host=config.host, port=config.port, user=config.user, password=config.password, database=config.database)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT payload FROM oa_auth_sessions WHERE username=%s", ("test-user",))
            payload = bytes(cursor.fetchone()[0])
            assert b"old-token" not in payload and b"fake-cookie" not in payload
    finally:
        connection.close()
    repository.save("another-user", credentials, "another-token")
    repository.save("test-user", credentials, "latest-token")
    repository.delete("test-user", old.revision)
    assert repository.load("test-user").token == "latest-token"
    assert repository.load("another-user").token == "another-token"
    repository.save("test-user", credentials, "old-token")
    renew = Mock(side_effect=lambda: repository.save("test-user", credentials, "new-token"))
    seen = []

    def operation(saved):
        seen.append(saved.token)
        if saved.token == "old-token":
            raise OaApiError(401)
        return {"result": "原任务已继续"}

    requests = AuthenticatedRequestService("test-user", repository, renew, Mock())
    assert requests.execute(operation)["result"] == "原任务已继续"
    assert seen == ["old-token", "new-token"]
    renew.assert_called_once()
    repository.delete("test-user")
    repository.delete("another-user")
    print("真实 MySQL：加密读写、最新覆盖、用户隔离、版本删除和 401 后继续原任务通过。")
    from auto_login_smoke import run_scenario
    run_scenario("slider", repository=repository)
    print("真实 Chrome → 登录检测 → MySQL 保存 → 数据库读取通过。")


if __name__ == "__main__":
    main()

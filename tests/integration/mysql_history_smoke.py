"""隔离 MySQL 冒烟测试；只使用虚构账号，不加载项目 .env。

使用独立测试容器，设置 MYSQL_SMOKE_PORT 后直接运行本文件。
数据库 oa_history_test，账号 oa_history_test，密码 history-test-only。
"""
from concurrent.futures import ThreadPoolExecutor
import os
import time
from unittest.mock import patch
from uuid import uuid4

from cryptography.fernet import Fernet
import pymysql

from oa_agent.config import MySQLConfig
from oa_agent.history.manager import ConversationHistory
from oa_agent.history.mysql_repository import MySQLHistoryRepository
from oa_agent.history.repository import ConversationDeleted, ConversationSummary, HistoryError, HistoryMessage
from oa_agent.sessions.tasks import ChatTaskRunner


def main():
    config = MySQLConfig(host="127.0.0.1", port=int(os.environ["MYSQL_SMOKE_PORT"]),
                         database="oa_history_test", user="oa_history_test", password="history-test-only",
                         encryption_key=Fernet.generate_key().decode())
    repository = MySQLHistoryRepository(config)
    deadline = time.monotonic() + 45
    while True:
        try:
            repository.list("alice")
            break
        except HistoryError:
            if time.monotonic() > deadline:
                raise
            time.sleep(1)

    history = ConversationHistory("alice", repository)
    conversation_id = history.current_id
    history.add(conversation_id, "user", "虚构员工的部门是什么？")
    history.add(conversation_id, "assistant", "虚构研发部")
    history.flush()
    assert not history.warning
    restored = ConversationHistory("alice", repository)
    assert restored.refresh()
    assert restored.messages() == history.messages()
    assert not repository.list("bob")
    for operation in (lambda: repository.load("bob", conversation_id),
                      lambda: repository.create("bob", restored.summaries[0]),
                      lambda: repository.append("bob", conversation_id, []),
                      lambda: repository.delete("bob", conversation_id)):
        try:
            operation()
        except ConversationDeleted:
            pass
        else:
            raise AssertionError("账号隔离失效")

    messages = repository.load("alice", conversation_id).messages
    repository.append("alice", conversation_id, messages)
    assert len(repository.load("alice", conversation_id).messages) == 2

    # 中途失败必须回滚，第一条合法消息也不能留下。
    try:
        repository.append("alice", conversation_id, [HistoryMessage(uuid4().hex, "user", "待回滚", 2),
                                                     HistoryMessage(uuid4().hex, "system", "无效角色", 3)])
    except HistoryError:
        pass
    else:
        raise AssertionError("无效消息未触发回滚")
    assert len(repository.load("alice", conversation_id).messages) == 2

    def append(index):
        repo = MySQLHistoryRepository(config)
        repo.append("alice", conversation_id, [HistoryMessage(uuid4().hex, "assistant", f"并发消息{index}", 4 + index)])

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(append, range(8)))
    assert len(repository.load("alice", conversation_id).messages) == 10

    connection = pymysql.connect(host=config.host, port=config.port, database=config.database,
                                 user=config.user, password=config.password, autocommit=True)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT id, payload FROM oa_chat_messages WHERE conversation_id=%s ORDER BY sequence_number", (conversation_id,))
            rows = cursor.fetchall()
            assert all("虚构".encode() not in bytes(row[1]) for row in rows)
            # 把密文复制到同会话另一消息，也会因消息 ID 绑定而拒绝读取。
            cursor.execute("UPDATE oa_chat_messages SET payload=%s WHERE id=%s", (rows[0][1], rows[1][0]))
        try:
            repository.load("alice", conversation_id)
        except HistoryError:
            pass
        else:
            raise AssertionError("密文绑定失效")
        summary = restored.summaries[0]
        repository.delete("alice", conversation_id)
        try:
            repository.create("alice", summary)
        except ConversationDeleted:
            pass
        else:
            raise AssertionError("删除后被重试复活")
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM oa_chat_messages WHERE conversation_id=%s", (conversation_id,))
            assert cursor.fetchone()[0] == 0
            cursor.execute("SELECT title, deleted_at FROM oa_chat_conversations WHERE id=%s", (conversation_id,))
            title, deleted_at = cursor.fetchone()
            assert not title and deleted_at is not None
    finally:
        connection.close()

    for index in range(32):
        repository.create("pagination", ConversationSummary(uuid4().hex, f"测试{index}", index, index))
    assert len(repository.list("pagination")) == 30
    assert len(repository.list("pagination", offset=30)) == 2
    assert repository.list("pagination")[0].title == "测试31"

    offline = ConversationHistory("offline-test", repository)
    runner = ChatTaskRunner(offline)
    calls = []
    with patch.object(repository, "create", side_effect=HistoryError("模拟断开")):
        runner.submit("虚构离线问题", lambda messages: calls.append(messages) or "离线完成")
        runner._future.result(timeout=5)
        runner.poll()
    offline.flush()
    assert len(repository.load("offline-test", offline.current_id).messages) == 2
    assert len(calls) == 1
    print("MySQL 历史记录：加密恢复、账号隔离、重试幂等、并发排序、事务回滚、密文绑定、删除防复活、分页和离线补存通过。")


if __name__ == "__main__":
    main()

"""按账号隔离的 MySQL 聊天历史，追加和删除均在会话行锁下执行。"""
from contextlib import contextmanager
import json
import time

from cryptography.fernet import Fernet, InvalidToken
import pymysql

from oa_agent.config import MySQLConfig
from oa_agent.history.repository import (
    Conversation, ConversationDeleted, ConversationSummary, HistoryError, HistoryMessage,
)


SCHEMA_SQL = (
    """CREATE TABLE IF NOT EXISTS oa_chat_conversations (
        id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
        username VARCHAR(191) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        title VARCHAR(120) NOT NULL,
        created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL, deleted_at DOUBLE NULL,
        INDEX idx_chat_owner_updated (username, deleted_at, updated_at, id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS oa_chat_messages (
        id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
        conversation_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        sequence_number BIGINT NOT NULL, role VARCHAR(16) NOT NULL,
        payload LONGBLOB NOT NULL, created_at DOUBLE NOT NULL,
        UNIQUE KEY idx_chat_message_order (conversation_id, sequence_number),
        CONSTRAINT fk_chat_conversation FOREIGN KEY (conversation_id) REFERENCES oa_chat_conversations(id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
)


class MySQLHistoryRepository:
    def __init__(self, config: MySQLConfig):
        self.config = config
        self._schema_ready = False

    @contextmanager
    def _transaction(self, username):
        connection = None
        try:
            if not username or not self.config.configured:
                raise HistoryError("历史记录暂未保存，请检查 OA_USERNAME、MySQL 配置和加密密钥。")
            options = dict(host=self.config.host, port=self.config.port, user=self.config.user,
                           password=self.config.password, database=self.config.database, charset="utf8mb4",
                           connect_timeout=5, read_timeout=10, write_timeout=10, autocommit=False,
                           cursorclass=pymysql.cursors.DictCursor)
            if self.config.ssl_ca:
                options.update(ssl_ca=self.config.ssl_ca, ssl_verify_cert=True, ssl_verify_identity=True)
            connection = pymysql.connect(**options)
            with connection.cursor() as cursor:
                if not self._schema_ready:
                    for statement in SCHEMA_SQL:
                        cursor.execute(statement)
                    self._schema_ready = True
                yield cursor
            connection.commit()
        except Exception as error:
            if connection is not None:
                try:
                    connection.rollback()
                except Exception:
                    pass
            if isinstance(error, HistoryError):
                raise
            if isinstance(error, InvalidToken):
                raise HistoryError("历史记录无法解密，请检查 SESSION_ENCRYPTION_KEY 是否与保存时一致。") from None
            raise HistoryError("历史记录读写失败，请检查 MySQL 连接、权限、表结构和加密密钥。") from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    @staticmethod
    def _summary(row):
        return ConversationSummary(**{key: row[key] for key in ("id", "title", "created_at", "updated_at")})

    @staticmethod
    def _owned(cursor, username, conversation_id):
        cursor.execute("""SELECT id FROM oa_chat_conversations
            WHERE id=%s AND username=%s AND deleted_at IS NULL FOR UPDATE""", (conversation_id, username))
        if not cursor.fetchone():
            raise ConversationDeleted()

    def list(self, username, limit=30, offset=0):
        with self._transaction(username) as cursor:
            cursor.execute("""SELECT id, title, created_at, updated_at FROM oa_chat_conversations
                WHERE username=%s AND deleted_at IS NULL ORDER BY updated_at DESC, id DESC LIMIT %s OFFSET %s""",
                           (username, limit, offset))
            return [self._summary(row) for row in cursor.fetchall()]

    def load(self, username, conversation_id):
        with self._transaction(username) as cursor:
            cursor.execute("""SELECT id, title, created_at, updated_at FROM oa_chat_conversations
                WHERE id=%s AND username=%s AND deleted_at IS NULL""", (conversation_id, username))
            row = cursor.fetchone()
            if not row:
                raise ConversationDeleted()
            conversation = Conversation(self._summary(row))
            cursor.execute("""SELECT id, role, payload, created_at FROM oa_chat_messages
                WHERE conversation_id=%s ORDER BY sequence_number""", (conversation_id,))
            cipher = Fernet(self.config.encryption_key.encode())
            for message in cursor.fetchall():
                data = json.loads(cipher.decrypt(bytes(message["payload"])))
                if (data["username"], data["conversation_id"], data["id"], data["role"]) != (
                        username, conversation_id, message["id"], message["role"]):
                    raise HistoryError("历史记录校验失败，无法读取该会话。")
                conversation.messages.append(HistoryMessage(message["id"], message["role"],
                                                           data["content"], message["created_at"]))
            return conversation

    def create(self, username, summary):
        with self._transaction(username) as cursor:
            # 已存在的删除标记绝不能由重试覆盖；账号校验在同一事务里执行。
            cursor.execute("""INSERT INTO oa_chat_conversations (id, username, title, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s) ON DUPLICATE KEY UPDATE id=id""",
                           (summary.id, username, summary.title, summary.created_at, summary.updated_at))
            self._owned(cursor, username, summary.id)

    def append(self, username, conversation_id, messages):
        with self._transaction(username) as cursor:
            self._owned(cursor, username, conversation_id)
            cursor.execute("SELECT COALESCE(MAX(sequence_number), 0) AS last_number FROM oa_chat_messages WHERE conversation_id=%s",
                           (conversation_id,))
            number = cursor.fetchone()["last_number"]
            cipher = Fernet(self.config.encryption_key.encode())
            for message in messages:
                if message.role not in {"user", "assistant"}:
                    raise HistoryError("历史消息格式无效。")
                cursor.execute("SELECT conversation_id FROM oa_chat_messages WHERE id=%s", (message.id,))
                existing = cursor.fetchone()
                if existing:
                    if existing["conversation_id"] != conversation_id:
                        raise HistoryError("历史消息标识冲突。")
                    continue
                number += 1
                data = dict(username=username, conversation_id=conversation_id, id=message.id,
                            role=message.role, content=message.content)
                payload = cipher.encrypt(json.dumps(data, ensure_ascii=False).encode())
                cursor.execute("""INSERT INTO oa_chat_messages
                    (id, conversation_id, sequence_number, role, payload, created_at) VALUES (%s, %s, %s, %s, %s, %s)""",
                               (message.id, conversation_id, number, message.role, payload, message.created_at))
                cursor.execute("UPDATE oa_chat_conversations SET updated_at=GREATEST(updated_at, %s) WHERE id=%s",
                               (message.created_at, conversation_id))

    def delete(self, username, conversation_id):
        with self._transaction(username) as cursor:
            cursor.execute("SELECT id FROM oa_chat_conversations WHERE id=%s AND username=%s FOR UPDATE",
                           (conversation_id, username))
            if not cursor.fetchone():
                raise ConversationDeleted()
            cursor.execute("DELETE FROM oa_chat_messages WHERE conversation_id=%s", (conversation_id,))
            cursor.execute("""UPDATE oa_chat_conversations SET title='', deleted_at=COALESCE(deleted_at, %s)
                WHERE id=%s AND username=%s""", (time.time(), conversation_id, username))

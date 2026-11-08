"""历史记录接口及隔离测试使用的内存仓库。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import threading
from typing import Literal, Protocol


class HistoryError(Exception):
    """只携带可以安全展示的固定提示，不公开数据库异常原文。"""


class ConversationDeleted(HistoryError):
    def __init__(self):
        super().__init__("会话不存在或已被删除。")


@dataclass
class ConversationSummary:
    id: str
    title: str
    created_at: float
    updated_at: float


@dataclass
class HistoryMessage:
    id: str
    role: Literal["user", "assistant"]
    content: str
    created_at: float


@dataclass
class Conversation:
    summary: ConversationSummary
    messages: list[HistoryMessage] = field(default_factory=list)


class HistoryRepository(Protocol):
    def list(self, username: str, limit: int = 30, offset: int = 0) -> list[ConversationSummary]: ...
    def load(self, username: str, conversation_id: str) -> Conversation: ...
    def create(self, username: str, summary: ConversationSummary) -> None: ...
    def append(self, username: str, conversation_id: str, messages: list[HistoryMessage]) -> None: ...
    def delete(self, username: str, conversation_id: str) -> None: ...


class InMemoryHistoryRepository:
    """仅用于测试；应用不会在数据库失败时自动替换仓库。"""

    def __init__(self):
        self.rows: dict[str, tuple[str, Conversation]] = {}
        self.deleted: set[str] = set()
        self._lock = threading.RLock()

    def list(self, username, limit=30, offset=0):
        with self._lock:
            rows = [row.summary for owner, row in self.rows.values()
                    if owner == username and row.summary.id not in self.deleted]
            return deepcopy(sorted(rows, key=lambda row: (row.updated_at, row.id), reverse=True)[offset:offset + limit])

    def load(self, username, conversation_id):
        with self._lock:
            owner, row = self.rows.get(conversation_id, (None, None))
            if owner != username or conversation_id in self.deleted:
                raise ConversationDeleted()
            return deepcopy(row)

    def create(self, username, summary):
        with self._lock:
            if summary.id not in self.rows:
                self.rows[summary.id] = (username, Conversation(deepcopy(summary)))
            self.load(username, summary.id)

    def append(self, username, conversation_id, messages):
        with self._lock:
            self.load(username, conversation_id)
            row = self.rows[conversation_id][1]
            existing = {message.id for message in row.messages}
            for message in messages:
                if message.id not in existing:
                    row.messages.append(deepcopy(message))
                    row.summary.updated_at = max(row.summary.updated_at, message.created_at)
                    existing.add(message.id)

    def delete(self, username, conversation_id):
        with self._lock:
            owner, row = self.rows.get(conversation_id, (None, None))
            if owner != username:
                raise ConversationDeleted()
            row.messages.clear()
            row.summary.title = ""
            self.deleted.add(conversation_id)

"""页面内的会话缓存与待保存队列；后台线程可独立完成落库。"""
from copy import deepcopy
import threading
import time
from uuid import uuid4

from oa_agent.agents.state import ChatMessage
from oa_agent.history.repository import (
    Conversation, ConversationDeleted, ConversationSummary, HistoryError, HistoryMessage, HistoryRepository,
)


class ConversationHistory:
    PAGE_SIZE = 30
    RETRY_SECONDS = 10

    def __init__(self, username: str = "", repository: HistoryRepository | None = None):
        self.username = username.strip()
        self.repository = repository
        self.limit = self.PAGE_SIZE
        self._lock = threading.RLock()
        self._io_lock = threading.RLock()
        self._cache: dict[str, Conversation] = {}
        self._listed: dict[str, ConversationSummary] = {}
        self._pending: dict[str, list[HistoryMessage]] = {}
        self._has_more = False
        self._save_error = ""
        self._read_error = ""
        self._restore_latest = True
        self._retry_running = False
        self._retry_timer = None
        self._last_retry = time.monotonic()
        self._revision = 0
        self._new()

    @property
    def persistent(self):
        return bool(self.username and self.repository is not None)

    @property
    def revision(self):
        with self._lock:
            return self._revision

    @property
    def warning(self):
        with self._lock:
            if not self.persistent:
                return "当前仅保留临时聊天；配置 OA_USERNAME 后可保存历史记录。"
            parts = [self._save_error, self._read_error]
            if self._pending:
                parts.append("部分聊天记录尚未保存，关闭页面或重启可能丢失；系统每 10 秒重试。")
            return " ".join(part for part in parts if part)

    @property
    def has_more(self):
        with self._lock:
            return self._has_more or len(self._all_summaries()) > self.limit

    @property
    def summaries(self):
        with self._lock:
            return deepcopy(self._all_summaries()[:self.limit])

    def _all_summaries(self):
        rows = dict(self._listed)
        rows.update({key: row.summary for key, row in self._cache.items() if row.messages})
        return sorted(rows.values(), key=lambda row: (row.updated_at, row.id), reverse=True)

    def _new(self):
        now = time.time()
        row = Conversation(ConversationSummary(uuid4().hex, "新会话", now, now))
        self._cache[row.summary.id] = row
        self.current_id = row.summary.id
        self._revision += 1
        return self.current_id

    def new(self):
        with self._lock:
            self._restore_latest = False
            # 未发送的空白页无需持续占用缓存。
            current = self._cache.get(self.current_id)
            if current and not current.messages:
                self._cache.pop(self.current_id)
            return self._new()

    def messages(self, conversation_id=None) -> list[ChatMessage]:
        with self._lock:
            row = self._cache[conversation_id or self.current_id]
            return [{"role": message.role, "content": message.content} for message in row.messages]

    def add(self, conversation_id, role, content):
        with self._lock:
            row = self._cache.get(conversation_id)
            if row is None:
                return False
            message = HistoryMessage(uuid4().hex, role, content, time.time())
            if not row.messages:
                row.summary.title = " ".join(content.split())[:30] or "新会话"
            row.messages.append(message)
            row.summary.updated_at = message.created_at
            self._restore_latest = False
            if self.persistent:
                self._pending.setdefault(conversation_id, []).append(message)
            self._revision += 1
            return True

    def contains(self, conversation_id):
        with self._lock:
            return conversation_id in self._cache

    def refresh(self):
        if not self.persistent:
            return True
        with self._io_lock:
            try:
                rows = self.repository.list(self.username, self.limit + 1)
                with self._lock:
                    self._listed = {row.id: row for row in rows[:self.limit]}
                    self._has_more = len(rows) > self.limit
                    self._read_error = ""
                    restore = self._restore_latest
                if restore:
                    restored = self.repository.load(self.username, rows[0].id) if rows else None
                    with self._lock:
                        # 用户可能在后台读库期间新建或发送了消息，不能覆盖其当前页面。
                        if self._restore_latest:
                            if restored:
                                self._cache[restored.summary.id] = restored
                                self.current_id = restored.summary.id
                            self._restore_latest = False
                with self._lock:
                    self._revision += 1
                return True
            except HistoryError as error:
                with self._lock:
                    self._read_error = str(error)
                self._schedule_retry()
                return False

    def load_more(self):
        with self._lock:
            self.limit += self.PAGE_SIZE
        return self.refresh()

    def select(self, conversation_id):
        with self._io_lock:
            with self._lock:
                cached = conversation_id in self._cache
            if not cached:
                try:
                    row = self.repository.load(self.username, conversation_id)
                except HistoryError as error:
                    with self._lock:
                        self._read_error = str(error)
                    return False
                with self._lock:
                    self._cache[conversation_id] = row
            with self._lock:
                self.current_id = conversation_id
                self._restore_latest = False
                self._read_error = ""
                self._revision += 1
            return True

    def flush(self):
        """仅持久化队列快照；新追加的消息由下一次保存处理。"""
        if not self.persistent:
            return
        with self._io_lock:
            with self._lock:
                pending = {key: (deepcopy(self._cache[key].summary), deepcopy(messages))
                           for key, messages in self._pending.items()}
            failed = False
            for conversation_id, (summary, messages) in pending.items():
                try:
                    self.repository.create(self.username, summary)
                    self.repository.append(self.username, conversation_id, messages)
                except ConversationDeleted:
                    with self._lock:
                        self._forget(conversation_id)
                        self._read_error = "会话已在其他页面删除，未重新创建该会话。"
                except HistoryError as error:
                    failed = True
                    with self._lock:
                        self._save_error = str(error)
                else:
                    saved_ids = {message.id for message in messages}
                    with self._lock:
                        remaining = [message for message in self._pending.get(conversation_id, [])
                                     if message.id not in saved_ids]
                        if remaining:
                            self._pending[conversation_id] = remaining
                        else:
                            self._pending.pop(conversation_id, None)
            with self._lock:
                if not failed:
                    self._save_error = ""
                self._revision += 1
            self._schedule_retry()

    def _forget(self, conversation_id):
        self._pending.pop(conversation_id, None)
        self._cache.pop(conversation_id, None)
        self._listed.pop(conversation_id, None)
        if self.current_id == conversation_id:
            self._new()
        self._revision += 1

    def delete(self, conversation_id):
        with self._io_lock:
            with self._lock:
                row = deepcopy(self._cache.get(conversation_id))
            try:
                if self.persistent:
                    if row:
                        self.repository.create(self.username, row.summary)
                    self.repository.delete(self.username, conversation_id)
            except ConversationDeleted:
                pass
            except HistoryError as error:
                with self._lock:
                    self._read_error = "删除未完成。" + str(error)
                return False
            with self._lock:
                self._forget(conversation_id)
                self._read_error = ""
                self._restore_latest = False
            return True

    def retry(self, force=False):
        """界面定时触发后台补存，不在 Streamlit 渲染线程等待数据库超时。"""
        with self._lock:
            now = time.monotonic()
            if (not self.persistent or self._retry_running
                    or not force and (now - self._last_retry < self.RETRY_SECONDS
                                      or not (self._pending or self._read_error or self._save_error))):
                return False
            self._last_retry = now
            self._retry_running = True

        def work():
            try:
                self.flush()
                self.refresh()
            finally:
                with self._lock:
                    self._retry_running = False
                    self._revision += 1
                self._schedule_retry()

        threading.Thread(target=work, daemon=True).start()
        return True

    def _schedule_retry(self):
        # 即使页面已关闭，仍存活的进程也能继续补存；Timer 不接触 Streamlit。
        with self._lock:
            if (not self.persistent or self._retry_timer is not None
                    or not (self._pending or self._read_error or self._save_error)):
                return

            def retry_later():
                with self._lock:
                    self._retry_timer = None
                self.retry(force=True)

            self._retry_timer = threading.Timer(self.RETRY_SECONDS, retry_later)
            self._retry_timer.daemon = True
            self._retry_timer.start()

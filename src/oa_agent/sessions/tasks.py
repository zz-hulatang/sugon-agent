"""保存当前聊天任务与结果，页面重跑时不重复调用模型或丢失等待验证的任务。"""
from concurrent.futures import Future
from copy import deepcopy
import threading
import time
from uuid import uuid4
from oa_agent.history.manager import ConversationHistory
from oa_agent.schemas.progress import ProgressEvent, ProgressStep, StepUpdate, TaskProgress, ThinkingEntry


class ChatTaskRunner:
    def __init__(self, history: ConversationHistory | None = None):
        self.history = history if history is not None else ConversationHistory()
        self._future = None
        self._lock = threading.Lock()
        self.running_conversation_id = None
        self._progress: dict[str, list[TaskProgress]] = {}

    def progress(self, conversation_id=None):
        """页面只读取副本；任务完成后仍保留本进程内的执行过程。"""
        with self._lock:
            return deepcopy(self._progress.get(conversation_id or self.history.current_id, []))

    @property
    def messages(self):
        return self.history.messages()

    @property
    def busy(self):
        return self._future is not None

    def submit(self, prompt, respond, *, streaming=False):
        with self._lock:
            if self.busy:
                return False
            conversation_id = self.history.current_id
            self.history.add(conversation_id, "user", prompt)
            history = self.history.messages(conversation_id)
            future = self._future = Future()
            self.running_conversation_id = conversation_id
            progress = TaskProgress(uuid4().hex, conversation_id, len(history) - 1)
            self._progress.setdefault(conversation_id, []).append(progress)

        def emit(event: ProgressEvent):
            with self._lock:
                if progress.state != "running":
                    return
                if event.kind == "step":
                    step = next((step for step in progress.steps if step.key == event.key), None)
                    if step is None:
                        step = ProgressStep(event.key, event.text)
                        progress.steps.append(step)
                        if event.category in {"tool", "auth"}:
                            progress.thinking.append(ThinkingEntry(event.category, event.key))
                    if not step.updates or (step.updates[-1].text, step.updates[-1].state) != (event.text, event.state):
                        step.updates.append(StepUpdate(event.text, event.state))
                    step.text, step.state = event.text, event.state
                    step.category = event.category
                    if event.details:
                        step.details = tuple(dict.fromkeys((*step.details, *event.details)))
                    if event.state != "running":
                        step.finished_at = time.monotonic()
                elif event.kind == "answer_reset":
                    progress.answer = ""
                elif event.kind == "answer_delta":
                    progress.answer += event.text
                elif event.kind == "answer_set":
                    progress.answer = event.text
                elif event.kind == "summary_delta":
                    progress.summary += event.text
                    key = event.key or "model"
                    progress.summaries[key] = progress.summaries.get(key, "") + event.text
                    entry = next((entry for entry in progress.thinking if entry.kind == "summary" and entry.key == key), None)
                    if entry is None:
                        entry = ThinkingEntry("summary", key)
                        progress.thinking.append(entry)
                    entry.text += event.text
                elif event.kind == "commentary":
                    entry = next((entry for entry in progress.thinking if entry.kind == "commentary" and entry.key == event.key), None)
                    if entry is None:
                        entry = ThinkingEntry("commentary", event.key)
                        progress.thinking.append(entry)
                    entry.text = event.text

        def work():
            # 后台线程不访问 st.session_state，也不执行任何 Streamlit 渲染。
            self.history.flush()
            if not self.history.contains(conversation_id):
                with self._lock:
                    self._progress.pop(conversation_id, None)
                future.set_result(None)
                return
            state = "complete"
            try:
                response = respond(history, emit) if streaming else respond(history)
            except Exception:
                response = "任务处理失败，请检查模型、数据库和 OA 网络。"
                state = "error"
            with self._lock:
                progress.answer = response
                progress.state = state
                progress.finished_at = time.monotonic()
                for step in progress.steps:
                    if step.state == "running":
                        step.state = "error" if state == "error" else "complete"
                        step.finished_at = progress.finished_at
            self.history.add(conversation_id, "assistant", response)
            self.history.flush()
            future.set_result(response)

        threading.Thread(target=work, daemon=True).start()
        return True

    def poll(self):
        with self._lock:
            if self._future is not None and self._future.done():
                self._future = None
                self.running_conversation_id = None
                return True
        return False

    def delete_conversation(self, conversation_id):
        with self._lock:
            if self.busy and conversation_id == self.running_conversation_id:
                return False
            deleted = self.history.delete(conversation_id)
            if deleted:
                self._progress.pop(conversation_id, None)
            return deleted

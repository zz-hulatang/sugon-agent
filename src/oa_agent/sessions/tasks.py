"""保存当前聊天任务与结果，页面重跑时不重复调用模型或丢失等待验证的任务。"""
from concurrent.futures import Future
import threading
from oa_agent.history.manager import ConversationHistory


class ChatTaskRunner:
    def __init__(self, history: ConversationHistory | None = None):
        self.history = history if history is not None else ConversationHistory()
        self._future = None
        self._lock = threading.Lock()
        self.running_conversation_id = None

    @property
    def messages(self):
        return self.history.messages()

    @property
    def busy(self):
        return self._future is not None

    def submit(self, prompt, respond):
        with self._lock:
            if self.busy:
                return False
            conversation_id = self.history.current_id
            self.history.add(conversation_id, "user", prompt)
            history = self.history.messages(conversation_id)
            future = self._future = Future()
            self.running_conversation_id = conversation_id

        def work():
            # 后台线程不访问 st.session_state，也不执行任何 Streamlit 渲染。
            self.history.flush()
            if not self.history.contains(conversation_id):
                future.set_result(None)
                return
            try:
                response = respond(history)
            except Exception:
                response = "任务处理失败，请检查模型、数据库和 OA 网络。"
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
            return self.history.delete(conversation_id)

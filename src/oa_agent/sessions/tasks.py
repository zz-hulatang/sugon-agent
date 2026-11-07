"""保存当前聊天任务与结果，页面重跑时不重复调用模型或丢失等待验证的任务。"""
from concurrent.futures import Future
from copy import deepcopy
import threading
from oa_agent.agents.state import ChatMessage


class ChatTaskRunner:
    def __init__(self):
        self.messages: list[ChatMessage] = []
        self._future = None
        self._lock = threading.Lock()

    @property
    def busy(self):
        return self._future is not None

    def submit(self, prompt, respond):
        with self._lock:
            if self.busy:
                return False
            self.messages.append(ChatMessage(role="user", content=prompt))
            history = deepcopy(self.messages)
            future = self._future = Future()

        def work():
            # 后台线程不访问 st.session_state，也不执行任何 Streamlit 渲染。
            try:
                future.set_result(respond(history))
            except Exception:
                future.set_result("任务处理失败，请检查模型、数据库和 OA 网络。")

        threading.Thread(target=work, daemon=True).start()
        return True

    def poll(self):
        with self._lock:
            if self._future is not None and self._future.done():
                self.messages.append(ChatMessage(role="assistant", content=self._future.result()))
                self._future = None

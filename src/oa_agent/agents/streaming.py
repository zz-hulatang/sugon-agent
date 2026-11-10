"""把 Agent 流和回调转换为详细执行轨迹，不公开认证数据或异常原文。"""
from contextlib import nullcontext

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, ToolMessage

from oa_agent.schemas.progress import ProgressEvent
from oa_agent.agents.trace_details import FAILURE_STATUSES, input_details, result_details


TOOL_LABELS = {
    "oa_login": "OA 登录操作",
    "get_user_info": "查询用户信息",
    "get_attendance_exceptions": "查询异常考勤",
    "submit_abnormal_attendance": "提交异常考勤审批",
}


def answer_text(message) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    return "".join(block.get("text", "") for block in content
                   if isinstance(block, dict) and block.get("type") in {"text", "output_text"})


def summary_text(message) -> str:
    """只读取接口显式返回的 summary_text，不读取内部推理或加密内容。"""
    if not isinstance(message.content, list):
        return ""
    return "".join(part.get("text", "") for block in message.content
                   if isinstance(block, dict) and block.get("type") == "reasoning"
                   for part in block.get("summary", [])
                   if isinstance(part, dict) and part.get("type") == "summary_text")


class ProgressCallbacks(BaseCallbackHandler):
    def __init__(self, emit):
        self.emit = emit
        self._tools = {}
        self._round = 0
        self._model_rounds = {}

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        self._round += 1
        self._model_rounds[str(run_id)] = self._round
        self.emit(ProgressEvent("answer_reset"))
        self.emit(ProgressEvent("step", str(run_id), f"第 {self._round} 轮模型分析", category="model",
                                details=("正在处理对话上下文，并生成答复或工具调用。",)))

    def on_llm_end(self, response, *, run_id, **kwargs):
        number = self._model_rounds.pop(str(run_id), self._round)
        details = []
        names = []
        for generations in response.generations:
            for generation in generations:
                message = getattr(generation, "message", None)
                if not isinstance(message, AIMessage):
                    continue
                names.extend(TOOL_LABELS.get(call.get("name"), "工具操作") for call in message.tool_calls)
                if message.tool_calls and answer_text(message).strip():
                    details.append("操作说明：" + answer_text(message)[:1500])
                    self.emit(ProgressEvent("commentary", key=str(message.id or run_id), text=answer_text(message)))
                usage = message.usage_metadata or {}
                if type(usage.get("output_tokens")) is int:
                    details.append(f"模型报告的输出用量：{usage['output_tokens']} token")
        if names:
            details.append(f"本轮选择了 {len(names)} 项工具操作：" + "、".join(names))
        label = "已选择工具操作" if names else "已生成答复"
        self.emit(ProgressEvent("step", str(run_id), f"第 {number} 轮模型：{label}", "complete",
                                category="model", details=tuple(details)))

    def on_llm_error(self, error, *, run_id, **kwargs):
        self.emit(ProgressEvent("step", str(run_id), "模型调用失败，请检查模型配置或网络", "error", category="model"))

    def on_tool_start(self, serialized, input_str, *, run_id, **kwargs):
        name = (serialized or {}).get("name")
        label = TOOL_LABELS.get(name, "执行工具")
        self._tools[str(run_id)] = (name, label)
        inputs = kwargs.get("inputs")
        self.emit(ProgressEvent("step", str(run_id), label, category="tool",
                                details=input_details(name, input_str if inputs is None else inputs)))

    def on_tool_end(self, output, *, run_id, **kwargs):
        name, label = self._tools.pop(str(run_id), (None, "执行工具"))
        if isinstance(output, ToolMessage):
            output = output.content
        status, details = result_details(name, output)
        failure = status in FAILURE_STATUSES
        result = "需要处理，请查看回复" if failure else "已返回结果"
        self.emit(ProgressEvent("step", str(run_id), f"{label}：{result}", "error" if failure else "complete",
                                category="tool", details=details))

    def on_tool_error(self, error, *, run_id, **kwargs):
        _name, label = self._tools.pop(str(run_id), (None, "执行工具"))
        self.emit(ProgressEvent("step", str(run_id), f"{label}：执行失败", "error", category="tool"))


def stream_agent_response(agent, messages, emit, session=None) -> str:
    """只执行一次 Agent；消费过程中出错也不回退重跑，防止重复提交 OA。"""
    final_answer = None
    listener = session.progress_listener(emit) if session is not None else nullcontext()
    with listener:
        for mode, payload in agent.stream(
            {"messages": messages},
            config={"recursion_limit": 10, "callbacks": [ProgressCallbacks(emit)]},
            stream_mode=["messages", "updates"],
        ):
            if mode == "messages":
                message, _metadata = payload
                if not isinstance(message, AIMessage):
                    continue
                text = answer_text(message)
                if text:
                    emit(ProgressEvent("answer_delta", text=text))
                summary = summary_text(message)
                if summary:
                    emit(ProgressEvent("summary_delta", key=str(message.id or "model"), text=summary))
            elif mode == "updates":
                for update in payload.values():
                    if not isinstance(update, dict):
                        continue
                    for message in update.get("messages", []):
                        if not isinstance(message, AIMessage):
                            continue
                        if message.tool_calls:
                            # 工具调用前的说明不是最终答复。
                            final_answer = None
                            emit(ProgressEvent("answer_reset"))
                        else:
                            final_answer = answer_text(message)
                            emit(ProgressEvent("answer_set", text=final_answer))
    if not final_answer:
        raise RuntimeError("模型未返回最终回答")
    return final_answer

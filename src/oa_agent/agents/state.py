"""定义聊天消息结构；认证凭据独立保存在会话层，不写入聊天历史。"""
from typing import Literal, TypedDict


class ChatMessage(TypedDict):
    """供 Streamlit 展示并传入 Agent 的用户/助手文本消息。"""

    role: Literal["user", "assistant"]
    content: str

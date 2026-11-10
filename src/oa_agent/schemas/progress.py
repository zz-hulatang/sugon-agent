"""页面展示事件，只携带经过筛选的业务详情，不携带认证数据。"""
from dataclasses import dataclass, field
import time
from typing import Literal


@dataclass(frozen=True)
class ProgressEvent:
    kind: Literal["step", "answer_delta", "answer_set", "answer_reset", "summary_delta", "commentary"]
    key: str = ""
    text: str = ""
    state: Literal["running", "complete", "error"] = "running"
    category: Literal["model", "tool", "auth", "info"] = "info"
    details: tuple[str, ...] = ()


@dataclass
class StepUpdate:
    text: str
    state: str
    at: float = field(default_factory=time.monotonic)


@dataclass
class ThinkingEntry:
    kind: Literal["summary", "commentary", "tool", "auth"]
    key: str
    text: str = ""


@dataclass
class ProgressStep:
    key: str
    text: str
    state: str = "running"
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    category: str = "info"
    details: tuple[str, ...] = ()
    updates: list[StepUpdate] = field(default_factory=list)


@dataclass
class TaskProgress:
    run_id: str
    conversation_id: str
    user_index: int
    state: str = "running"
    steps: list[ProgressStep] = field(default_factory=list)
    answer: str = ""
    summary: str = ""
    summaries: dict[str, str] = field(default_factory=dict)
    thinking: list[ThinkingEntry] = field(default_factory=list)
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None

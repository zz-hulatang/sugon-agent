"""员工信息接口与工具返回结构，保留服务端业务字段。"""
from typing import Any, Literal, NotRequired, TypedDict


class EmployeeResult(TypedDict):
    status: Literal["success", "auth_required", "config_error", "error"]
    message: str
    data: NotRequired[dict[str, Any]]
    status_code: NotRequired[int]

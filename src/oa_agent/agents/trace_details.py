"""只提取执行轨迹需要的业务参数和结果统计，不返回完整工具输入输出。"""
import json
import re


FAILURE_STATUSES = {
    "error", "config_error", "input_error", "invalid_request", "auth_required",
    "unknown", "manual_required", "expired", "stale",
}
STATUS_LABELS = {
    "success": "成功", "verified": "登录已验证", "waiting_login": "等待登录",
    "closed": "会话已关闭", "error": "执行失败", "config_error": "配置需要补充",
    "input_error": "输入需要修正", "invalid_request": "请求参数无效",
    "auth_required": "登录验证未完成", "unknown": "结果待核实，不能自动重试",
    "manual_required": "需要到 OA 人工处理", "expired": "查询快照已过期",
    "stale": "原查询记录已变化",
}


def object_value(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def account_name(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.@-]{1,80}", value) else None


def input_details(name, inputs) -> tuple[str, ...]:
    data = object_value(inputs)
    if name == "get_user_info":
        username = account_name(data.get("username"))
        target = username or ("本机配置账号" if data.get("username") is None else "已指定账号（格式未展开）")
        return (f"查询目标：{target}", "认证方式：复用本机登录会话")
    if name == "get_attendance_exceptions":
        year, month = data.get("year"), data.get("month")
        if type(year) is int and type(month) is int and 1000 <= year <= 9999 and 1 <= month <= 12:
            return (f"查询范围：{year} 年 {month} 月", "查询对象：本人异常考勤")
    if name == "submit_abnormal_attendance":
        items = data.get("items")
        if isinstance(items, list):
            return (f"选定记录：{len(items)} 条", "操作：提交 OA 审批")
    if name == "oa_login":
        action = data.get("action", "open")
        if not isinstance(action, str):
            return ()
        return ({"open": ("操作：打开 OA 登录页面",), "status": ("操作：读取本地会话状态",)}.get(action, ()))
    return ()


def result_details(name, output) -> tuple[str, tuple[str, ...]]:
    data = object_value(output)
    status = data.get("status", data.get("state", ""))
    status = status if isinstance(status, str) else ""
    details = ["返回状态：" + STATUS_LABELS.get(status, "工具已返回")]
    status_code = data.get("status_code")
    if type(status_code) is int and 100 <= status_code <= 599:
        details.append(f"HTTP 状态：{status_code}")
    if name == "get_user_info" and status == "success" and isinstance(data.get("data"), dict):
        profile = data["data"]
        username = account_name(profile.get("uname"))
        if username:
            details.append(f"返回账号：{username}")
        details.append(f"返回员工信息字段：{len(profile)} 项；回答将按问题选用")
    elif name == "get_attendance_exceptions" and status == "success":
        for field, label in (("records", "异常记录"), ("offset_types", "可用处理类别")):
            if isinstance(data.get(field), list):
                details.append(f"{label}：{len(data[field])} 项")
    elif name == "submit_abnormal_attendance" and status == "success":
        details.append("已提交审批，审批结果以 OA 为准")
    return status, tuple(details)

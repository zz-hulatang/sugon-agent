"""异常考勤查询和提交的 LangChain 工具适配。"""
from langchain.tools import tool

from oa_agent.schemas.attendance import AttendanceCorrection


def make_attendance_tools(session):
    @tool
    def get_attendance_exceptions(year: int, month: int) -> dict:
        """查询本人指定年月的异常考勤及允许的抵消类别，不提交工单。

        year、month 由模型结合当前日期和对话推断，用户无需给出完整年月。
        本月/上月等直接换算；未提时间时沿用最近的考勤查询月份，没有上下文则默认本月。
        能推断时直接调用，不要因参数必填追问用户；答复时说明实际查询年月。
        年份限上一年、当年、下一年，月份 1-12。返回有效十分钟的 draft_id、
        记录 record_id 及 rq 日期、dk/dk2 打卡时间、lx 类型、qjxs 小时、qjt 天数。
        账号和工作地由后台认证确定，不接受他人账号或凭据参数。
        """
        try:
            return session.call("attendance_query", {"year": year, "month": month})
        except Exception:
            return {"status": "error", "message": "异常考勤查询失败，请检查 OA 会话和网络。"}

    @tool
    def submit_abnormal_attendance(draft_id: str, items: list[AttendanceCorrection]) -> dict:
        """将选定的本人异常考勤记录提交 OA 审批，会产生真实工单。

        先查询获取 draft_id、record_id 和 offset_types。仅在用户明确要求提交且给出
        抵消类别及必要真实信息后调用，不能猜测类别、时间或备注；只提交 items 中的记录。
        忘打卡/卡机故障需 actual_start/actual_end，卡机故障还需 device_location。
        需要证明附件或人工选审批人的申请须去 OA 办理。后台保留原页面业务校验。
        unknown 表示可能部分写入或已提交，禁止自动重试或重新查询后重复提交。
        success 才表示已提交审批，不表示审批通过；同一快照重复调用不会重复提交。
        """
        try:
            return session.call("attendance_submit", {"draft_id": draft_id,
                                                       "items": [item.model_dump() for item in items]})
        except Exception:
            return {"status": "unknown", "message": "未能获取提交结果，请先在 OA 核实；禁止自动重试。"}

    return [get_attendance_exceptions, submit_abnormal_attendance]

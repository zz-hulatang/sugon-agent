"""查询快照与一次性提交：写请求永不进入认证恢复执行器。"""
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import json
import secrets
import time

from pydantic import ValidationError

from oa_agent.clients.attendance_client import AttendancePageError, RECORD_FIELDS
from oa_agent.clients.oa_client import OaApiError
from oa_agent.schemas.attendance import AttendanceCorrection
from oa_agent.services.authenticated_request import LoginRecoveryError
from oa_agent.sessions.repository import SessionRepositoryError


@dataclass(repr=False)
class AttendanceDraft:
    year: int
    month: int
    created: float
    records: dict
    attempted: bool = False
    result: dict | None = None


def record_key(row):
    return json.dumps([row.get(field) for field in RECORD_FIELDS], ensure_ascii=False, sort_keys=True)


class AttendanceService:
    DRAFT_TTL = 600
    MAX_DRAFTS = 10

    def __init__(self, config, client, requests):
        self.config, self.client, self.requests = config, client, requests
        self.drafts = {}

    def clear(self):
        self.drafts.clear()

    def _query(self, year, month):
        username = self.config.username.strip()
        return self.requests.execute(lambda saved: {
            "saved": saved,
            "query": self.client.query(username, year, month, saved.credentials.cookies)})

    def query(self, year, month):
        if not self.config.username.strip():
            return {"status": "config_error", "message": "请先在本机配置 OA_USERNAME，并重启应用。"}
        try:
            today = date.today()
            if type(year) is not int or type(month) is not int or year not in range(today.year - 1, today.year + 2):
                raise ValueError()
            date(year, month, 1)
        except (ValueError, TypeError):
            return {"status": "input_error", "message": "请选择上一年、当年或下一年的有效年月，与 OA 页面保持一致。"}
        try:
            data = self._query(year, month)["query"]
            now = time.monotonic()
            self.drafts = {key: value for key, value in self.drafts.items() if now - value.created < self.DRAFT_TTL}
            if len(self.drafts) >= self.MAX_DRAFTS:
                return {"status": "error", "message": "查询快照过多，请稍后重试。"}
            draft_id = secrets.token_hex(16)
            records = {secrets.token_hex(8): deepcopy(row) for row in data["records"]}
            self.drafts[draft_id] = AttendanceDraft(year, month, now, records)
            return {"status": "success", "message": "已获取异常记录。请按真实情况选择记录、抵消类别和备注。",
                    "draft_id": draft_id, "expires_in": self.DRAFT_TTL, "year": year, "month": month,
                    "records": [{"record_id": key, **{f: row.get(f) for f in RECORD_FIELDS if f != "workday"}}
                                for key, row in records.items()],
                    "offset_types": [item["type"] for item in data["choices"]]}
        except (OaApiError, SessionRepositoryError, LoginRecoveryError, AttendancePageError) as error:
            return self._error(error)
        except (InvalidOperation, KeyError, TypeError):
            return {"status": "error", "message": "OA 返回的异常考勤数据无效。"}

    def submit(self, draft_id, items):
        draft = self.drafts.get(draft_id)
        if draft is None or time.monotonic() - draft.created >= self.DRAFT_TTL:
            return {"status": "expired", "message": "查询快照不存在或已过期，请重新查询。若此前提交结果待核实，先在 OA 检查原工单。"}
        if draft.attempted:
            return deepcopy(draft.result) if draft.result else {
                "status": "unknown", "message": "该快照已尝试提交，请先在 OA 核实，禁止自动重复提交。"}
        try:
            corrections = [AttendanceCorrection.model_validate(item) for item in items]
            keys = [item.record_id for item in corrections]
            if not keys or len(keys) != len(set(keys)) or any(key not in draft.records for key in keys):
                raise ValueError()
        except (ValidationError, TypeError, ValueError):
            return {"status": "input_error", "message": "记录标识、抵消类别或实际时间无效，请使用查询结果并补充完整的真实信息。"}
        try:
            # 仅这些只读请求可以在 401 后恢复登录。刷新后再核对记录、时长和字典。
            current = self._query(draft.year, draft.month)
            query, saved = current["query"], current["saved"]
            selected = []
            for key in keys:
                matches = [row for row in query["records"] if record_key(row) == record_key(draft.records[key])]
                if len(matches) != 1:
                    return {"status": "stale", "message": "异常记录已变化或无法唯一匹配，请重新查询后选择。"}
                selected.append(matches[0])
            for item in corrections:
                if sum(option["type"] == item.offset_type for option in query["choices"]) != 1:
                    return {"status": "input_error", "message": "抵消类别已变化或不唯一，请重新查询。"}
            proof_types = {"生育产假", "产检假", "流产假", "婚假", "节育手术假", "陪产假"}
            sick_days = sum(Decimal(str(row["qjt"])) for row, item in zip(selected, corrections) if item.offset_type == "病假")
            if sick_days > 2 or any(item.offset_type in proof_types for item in corrections):
                return {"status": "manual_required", "message": "该申请按 OA 规则需要证明附件，请在 OA 页面上传材料后提交；当前工具不上传附件。"}
        except (OaApiError, SessionRepositoryError, LoginRecoveryError, AttendancePageError) as error:
            return self._error(error)
        except (InvalidOperation, KeyError, TypeError):
            return {"status": "error", "message": "OA 返回的请假天数无效，未发起提交。"}
        # 从此处开始可能保存明细、占用编号或更新请假表；任何异常均禁止重放。
        draft.attempted = True
        draft.result = {"status": "unknown", "message": "提交结果待核实，请在 OA 检查是否已生成工单；禁止自动重试。"}
        try:
            result = self.client.submit(self.config.username.strip(), saved.credentials.cookies,
                                        draft.year, draft.month, selected,
                                        [item.model_dump() for item in corrections], query["choices"])
            draft.result = {"status": "success", "message": "异常考勤申请已提交审批。", **result}
        except Exception as error:
            # 包含 401、页面改版、超时和部分写入失败；不泄露原始异常，不自动重发。
            if isinstance(error, AttendancePageError) and str(error) in {"validation_failed", "manual_routing_required"}:
                draft.result = {"status": "manual_required", "message": "原 OA 页面校验未通过或需要人工选择审批人。明细可能已保存，请打开原表单核实后继续办理。"}
            document = getattr(self.client, "last_document", None)
            if isinstance(document, dict):
                draft.result.update(document)
        return deepcopy(draft.result)

    @staticmethod
    def _error(error):
        if isinstance(error, SessionRepositoryError):
            return {"status": "error", "message": error.public_message}
        if isinstance(error, LoginRecoveryError) or isinstance(error, OaApiError) and error.status_code == 401:
            return {"status": "auth_required", "message": "旧 OA 表单会话不可用，请完成 OA 登录；SIO 登录成功仍需验证旧 OA 会话。"}
        reasons = {"previous_period_unfinished": "上一考勤期间尚未完成处理，请先在 OA 查看。",
                   "identity_mismatch": "旧 OA 登录身份与本机配置账号不一致，请检查登录身份。",
                   "missing_workplace": "OA 未返回工作地编码，请联系人力或考勤管理员。"}
        return {"status": "error", "message": reasons.get(str(error), "获取异常考勤数据失败，请检查 OA 网络或接口返回。")}

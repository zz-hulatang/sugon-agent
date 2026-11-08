"""已核实的旧 OA 查询接口；提交执行原表单脚本，不自行拼接审批链。"""
from calendar import monthrange
from contextlib import contextmanager
from datetime import date
from math import isfinite
from pathlib import Path
from urllib.parse import urlsplit, parse_qs, quote

from oa_agent.clients.oa_client import OaApiError


FORM_URL = "https://soa.com.cn/appForm/OpenForm?fromid=abnormalattendapp"
BASE_URL = "https://soa.com.cn"
RECORD_FIELDS = ("rq", "dk", "dk2", "lx", "qjxs", "qjt", "workday")


class AttendancePageError(Exception):
    """只暴露固定的错误分类，不向模型回传页面、响应原文或凭据。"""


def flatten_records(payload):
    if not isinstance(payload, dict) or str(payload.get("code")) != "200":
        raise OaApiError(200)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise OaApiError(200)
    if data.get("lastNeedEndDate") is not None:
        raise AttendancePageError("previous_period_unfinished")
    result = []
    for group in data.values():
        rows = group.values() if isinstance(group, dict) else group if isinstance(group, list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                hours, days = float(row.get("qjxs", 0)), float(row.get("qjt", 0))
                if not isfinite(hours) or not isfinite(days) or days < 0:
                    raise OaApiError(200)
                if row.get("workday") and hours > 0:
                    if not all(key in row for key in ("rq", "lx", "qjxs", "qjt")):
                        raise OaApiError(200)
                    date.fromisoformat(row["rq"])
                    result.append(row)
            except (ValueError, TypeError):
                raise OaApiError(200) from None
    return result


class AttendanceClient:
    def __init__(self, browser):
        self.browser = browser

    @staticmethod
    def _json(request, path, *, method="get", **kwargs):
        response = None
        try:
            response = getattr(request, method)(
                BASE_URL + path, timeout=15000, max_redirects=0,
                fail_on_status_code=False, **kwargs)
            if response.status in (301, 302, 303, 307, 308, 401, 403):
                raise OaApiError(401)
            if response.status != 200:
                raise OaApiError(response.status)
            # 真实旧 OA 的 getPsn 返回 JSON，却声明 text/html；先按内容解析。
            # 只有实际登录页面才视为认证失效，其他无效内容不触发自动重新登录。
            try:
                data = response.json()
            except Exception:
                content = response.text().lower()
                if 'name="loginform"' in content or 'id="username"' in content:
                    raise OaApiError(401) from None
                raise OaApiError(200) from None
            if isinstance(data, dict) and str(data.get("code")) in {"401", "403"}:
                raise OaApiError(401)
            return data
        except OaApiError:
            raise
        except Exception:
            raise OaApiError() from None
        finally:
            if response is not None:
                try:
                    response.dispose()
                except Exception:
                    pass

    def query(self, username, year, month, cookies):
        """这是只读查询，不打开会自动写入临时明细的 OpenForm 页面。"""
        with self.browser.request_context(cookies) as request:
            identity = self._json(request, "/getPsn?extension", method="post", form={
                "type": "getLoginPsn", "getfield": "USRID;NAME2;ZZGZD;BEGDA"})
            if not isinstance(identity, list) or len(identity) != 1 or not isinstance(identity[0], dict):
                raise OaApiError(200)
            person = identity[0]
            if str(person.get("USRID", "")).casefold() != username.casefold():
                raise AttendancePageError("identity_mismatch")
            if not person.get("ZZGZD"):
                raise AttendancePageError("missing_workplace")
            start = f"{year:04d}-{month:02d}-01"
            joined = str(person.get("BEGDA", ""))
            if joined[:7] == start[:7] and joined > start:
                start = joined
            payload = self._json(request, "/gateway/attendance/abnormalattendapp/getAbnormalAttend", params={
                "starTime": start, "endTime": f"{year:04d}-{month:02d}-{monthrange(year, month)[1]}",
                "workplaceid": str(person["ZZGZD"]), "itcode": username, "postflag": "abnormalattendapp"})
            records = flatten_records(payload)
            options = self._json(request, "/appForm/GetViewData", params={
                "viewName": "v_attendandeductapp_config1", "viewTable": "v_attendandeductapp_config1",
                "pageNum": 1, "pageSize": 1000, "getType": "all", "deltype": "true"})
            try:
                choices = [{"type": item["type"], "typecode": item["typecode"]}
                           for item in options["data"]["records"]]
                if not choices or any(not c["type"] or not c["typecode"] for c in choices):
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                raise OaApiError(200) from None
            return {"records": records, "choices": choices}

    @contextmanager
    def _form(self, cookies):
        with self.browser.business_page(cookies) as page:
            # 不向模型、日志或测试录制中输出浏览器异常及页面原文。
            page.on("dialog", lambda dialog: dialog.dismiss())
            response = page.goto(FORM_URL, wait_until="domcontentloaded", timeout=25000)
            if response is None or response.status != 200:
                raise AttendancePageError("form_unavailable")
            if page.locator("form[name='loginform'], #username").count():
                raise AttendancePageError("legacy_auth_required")
            actual = urlsplit(page.url)
            if (actual.scheme != "https" or actual.hostname != "soa.com.cn" or
                    actual.path != "/appForm/OpenForm"):
                raise AttendancePageError("form_unavailable")
            page.wait_for_function("""() => window.FlowCtlData?.initialized >= 5 &&
                window.form_data?.basicinfo?.apppsnid && window.control?.info_v?.$refs?.tbl &&
                typeof window.AnnualleaveConfig !== 'undefined'""", timeout=30000)
            # 等待 OpenForm 自带的初次明细加载完成，避免与后面的明细替换交错。
            page.wait_for_load_state("networkidle", timeout=15000)
            yield page

    def submit(self, username, cookies, year, month, records, corrections, choices):
        """调用一次原流程；最终 setNodeMsg 的 STATE=ok 才表示提交成功。"""
        self.last_document = None
        with self._form(cookies) as page:
            applicant = page.evaluate("() => form_data.basicinfo.apppsnid")
            if not isinstance(applicant, str) or applicant.casefold() != username.casefold():
                raise AttendancePageError("identity_mismatch")
            docid = page.evaluate("() => form_data.setfile.DOCID")
            if not isinstance(docid, str) or not docid:
                raise AttendancePageError("form_unavailable")
            self.last_document = {"doc_id": docid, "url": FORM_URL + "&docid=" + quote(docid, safe="")}
            script = Path(__file__).with_name("attendance_submit.js").read_text()
            # 原页面含同步 XHR。先调度、立即返回，再用驱动端超时等待，避免 evaluate
            # 随网络请求无限等待。这里只有可信的本地代码拼接，业务值仍单独传参。
            page.evaluate("""args => {
                window.__oaAttendanceTask = {done: false, value: false};
                setTimeout(() => Promise.resolve().then(() => (""" + script + """)(args))
                    .then(value => { window.__oaAttendanceTask = {done: true, value}; })
                    .catch(() => { window.__oaAttendanceTask = {done: true, value: false}; }), 0);
            }""", {"year": year, "month": month, "records": records,
                   "corrections": corrections, "choices": choices})
            page.wait_for_function("() => window.__oaAttendanceTask?.done", timeout=45000)
            prepared = page.evaluate("() => window.__oaAttendanceTask.value")
            if prepared is not True:
                raise AttendancePageError("validation_failed")
            page.wait_for_function("""() => document.querySelector('.flowDig .flowDig_submit') &&
                FlowCtlData.nextflowactionmsg?.actMsg === 'TJ' &&
                FlowCtlData.nextflowactionmsg?.nodeMsg?.nodeId""", timeout=20000)
            automatic = page.evaluate("""() => {
                const next = FlowCtlData.nextflowactionmsg;
                const radios = document.querySelectorAll('.flowDig_apprPsn_input input[name="flowDig_apprPsn_radio"]');
                return FlowCtlData.nextNodeAllMsg?.linkMsg?.length === 1 &&
                    (!radios.length || [...radios].some(radio => radio.checked)) &&
                    (next.nodeMsg.nodeId === 'flowEnd' || !!next.psnMsg?.psnId);
            }""")
            if automatic is not True:
                raise AttendancePageError("manual_routing_required")
            serial = page.evaluate("() => form_data.basicinfo.SerialNumber")

            def is_submission(response):
                url = urlsplit(response.url)
                params = parse_qs(url.query)
                return (url.scheme == "https" and url.hostname == "soa.com.cn"
                        and url.path == "/FlowEngine/setNodeMsg"
                        and params.get("docid") == [docid]
                        and response.request.method == "POST")

            # 设置监听后才点击最终确定，既不漏接同步响应，也不重放写请求。
            with page.expect_response(is_submission, timeout=30000) as pending:
                page.evaluate("() => { setTimeout(() => FlowCtlData.setNodeAction(), 0); }")
            response = pending.value
            if response.status != 200 or response.json().get("STATE") != "ok":
                raise AttendancePageError("submission_unconfirmed")
            return {"doc_id": docid, "serial_number": serial,
                    "url": FORM_URL + "&docid=" + quote(docid, safe="")}

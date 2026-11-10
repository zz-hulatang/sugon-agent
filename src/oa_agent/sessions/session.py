"""管理每个用户的浏览器线程、命令队列、自动检测及会话过期清理。"""
import atexit
import queue
import threading
import time
import hashlib
import json
from copy import deepcopy
from concurrent.futures import Future
from contextlib import contextmanager
from uuid import uuid4

from oa_agent.clients.browser import BrowserClient
from oa_agent.clients.oa_client import OaClient
from oa_agent.clients.attendance_client import AttendanceClient
from oa_agent.config import LoginConfig, MySQLConfig
from oa_agent.schemas.auth import Credentials
from oa_agent.schemas.progress import ProgressEvent
from oa_agent.services.auth import AuthService
from oa_agent.services.auto_login import AutoLoginService
from oa_agent.services.employee import EmployeeService
from oa_agent.services.attendance import AttendanceService
from oa_agent.services.authenticated_request import AuthenticatedRequestService, LoginRecoveryError
from oa_agent.sessions.mysql_repository import MySQLSessionRepository
from oa_agent.sessions.repository import SessionRepository, SessionRepositoryError
from oa_agent.sessions.store import CredentialStore


class LoginSession:
    """每个 Streamlit 会话创建一个实例，避免放入全局缓存导致身份串用。"""

    def __init__(self, config: LoginConfig, database: MySQLConfig | None = None,
                 repository: SessionRepository | None = None):
        self.config = config
        self.browser = BrowserClient(config)
        self.store = CredentialStore()
        self.auth = AuthService(config, self.browser, self.store)
        self.auto_login = AutoLoginService(config, self.browser)
        self.repository = repository if repository is not None else MySQLSessionRepository(database or MySQLConfig.from_env())
        self.requests = AuthenticatedRequestService(config.username.strip(), self.repository, self._renew_login,
                                                    self._invalidate, self._restore_snapshot)
        self.employee = EmployeeService(config, OaClient(self.browser), self.requests)
        self.attendance = AttendanceService(config, AttendanceClient(self.browser), self.requests)
        self._published_fingerprint = None
        self._published_revision = None
        self._recovering = False
        self._stopping = False
        self._deferred = []
        self._persistence_error = False
        self._persistence_message = ""
        self._commands: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._thread = None
        self._last_check = 0.0
        self._progress_lock = threading.Lock()
        self._progress_listeners = []
        atexit.register(self.close)

    @contextmanager
    def progress_listener(self, callback):
        """为当前任务订阅固定的登录进度提示，跨浏览器线程传递展示事件。"""
        with self._progress_lock:
            self._progress_listeners.append(callback)
        try:
            yield
        finally:
            with self._progress_lock:
                self._progress_listeners.remove(callback)

    def _emit_progress(self, key, text, state="running"):
        with self._progress_lock:
            callbacks = list(self._progress_listeners)
        for callback in callbacks:
            try:
                callback(ProgressEvent("step", key, text, state, category="auth"))
            except Exception:
                # 展示端失败不影响认证和原业务请求，也不触发重试。
                pass

    def _start(self):
        # Streamlit 重跑或工具调用可能来自不同线程，只允许启动一个工作线程。
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._stopping = False
                self._thread = threading.Thread(target=self._worker, daemon=True)
                self._thread.start()

    def call(self, action: str, payload: dict | None = None):
        """通过队列调用浏览器线程；credentials 仅供可信 Python 后端使用。"""
        business_actions = {"user_info", "attendance_query", "attendance_submit"}
        if action not in {"open", "collect", "status", "clear", "credentials", *business_actions}:
            raise ValueError("不支持的会话操作")
        if payload is not None and (not isinstance(payload, dict) or action not in business_actions):
            raise ValueError("不支持的会话参数")
        self._start()
        # 调用者等待 Future，浏览器操作始终在同一线程执行，保持 Playwright 线程约束。
        future: Future = Future()
        self._commands.put((action, future, deepcopy(payload or {})))
        # 业务请求自身有 HTTP/数据库超时和登录等待上限，不在调用端提前丢弃仍等待验证的任务。
        return future.result(timeout=None if action in business_actions else 55)

    def credentials(self) -> Credentials:
        """返回凭据副本给后端业务代码；不得作为 LangChain 工具暴露给模型。"""
        return self.call("credentials")

    def close(self):
        """向所属线程发送停止命令，避免从调用线程直接关闭 Playwright。"""
        with self._lock:
            if not self._thread or not self._thread.is_alive():
                return
            future: Future = Future()
            self._commands.put(("stop", future, {}))
        try:
            future.result(timeout=5)
            self._thread.join(timeout=1)
        except Exception:
            pass

    def _worker(self):
        """串行执行 UI/工具命令，并在没有用户操作时持续自动检测登录。"""
        # 使用单调时钟计算时长，避免系统时间校准影响会话过期判断。
        last_used = time.monotonic()
        while not self._stopping:
            if self.browser.context:
                now = time.monotonic()
                if now - last_used > self.config.idle_seconds:
                    self._clear()
                    self.store.state = "expired"
                elif now - self._last_check >= 1:
                    # 每秒检查一次；常规采集和状态刷新不会无限延长凭据保留时间。
                    self._last_check = now
                    was_waiting = self.store.state == "waiting_login"
                    try:
                        self._login_tick()
                        if was_waiting and self.store.state == "verified":
                            # 刚完成登录时重新计时，给后续 OA 操作保留完整的使用窗口。
                            last_used = time.monotonic()
                    except SessionRepositoryError as error:
                        self._persistence_error = True
                        self._persistence_message = error.public_message
                        self.store.reset("error")
                    except Exception:
                        # 登录跳转可能销毁正在读取的 frame，清空旧快照并在下次重试。
                        self.store.reset("waiting_login")
            try:
                action, future, payload = self._commands.get(timeout=0.1)
            except queue.Empty:
                # 队列等待超时后继续分发浏览器事件；自动采集不依赖 UI 发出命令。
                if self.browser.context:
                    try:
                        if not self.browser.pump_events():
                            self._clear()
                    except Exception:
                        self._clear()
                continue
            try:
                # 即使外部持续查询状态，也先处理过期，避免队列繁忙阻止凭据清理。
                if self.browser.context and time.monotonic() - last_used > self.config.idle_seconds:
                    self._clear()
                    self.store.state = "expired"
                if action == "stop":
                    self.attendance.clear()
                    self._clear()
                    future.set_result(None)
                    return
                if action == "open":
                    last_used = time.monotonic()
                    if not self.browser.context:
                        self.store.state = "waiting_login"
                    self.browser.open(self.auth.on_request, self.auth.on_response)
                    result = self._status(
                        "已打开 OA，正在自动填写并提交登录；如需二次验证，请在浏览器中完成。"
                        if self.config.auto_login_enabled else
                        "已打开 OA，请在浏览器中登录；登录成功后将自动采集会话。"
                    )
                elif action == "collect":
                    # 保留后端主动刷新能力；用户界面和模型都不需要调用此操作。
                    last_used = time.monotonic()
                    result = self.auth.collect()
                    if self.store.state == "verified":
                        self._publish()
                elif action == "clear":
                    self.attendance.clear()
                    username = self.config.username.strip() or self.auth.authenticated_username
                    try:
                        if username:
                            self.repository.delete(username)
                    finally:
                        # 数据库离线时仍必须响应用户的本地关闭操作。
                        self._clear()
                    result = self._status("已清除当前用户的数据库会话和本地凭据；未调用 OA 服务端注销。")
                elif action == "credentials":
                    result = self.store.snapshot()
                elif action == "user_info":
                    # HTTP 调用同样在浏览器所属线程执行，避免跨线程使用 Playwright。
                    result = self.employee.get_user_info(**payload)
                elif action == "attendance_query":
                    result = self.attendance.query(**payload)
                elif action == "attendance_submit":
                    result = self.attendance.submit(**payload)
                else:
                    messages = {
                        "verified": "已自动采集登录会话。",
                        "waiting_login": "等待在浏览器中完成登录。",
                    }
                    result = self._status(messages.get(self.store.state, "这是本地会话状态。"))
                future.set_result(result)
            except Exception:
                # 原始浏览器异常可能含带 Token 的 SSO 地址，只返回固定的错误提示。
                if action == "open":
                    self._clear()
                elif action == "collect":
                    self.store.reset("waiting_login" if self.browser.context else "closed")
                future.set_exception(RuntimeError("OA 操作失败，请检查网络、浏览器及登录检测配置。"))

    def _status(self, message):
        if self._persistence_error:
            message = self._persistence_message or "MySQL 会话保存失败，请检查连接、权限及加密密钥。"
        result = self.store.status(message)
        result["auto_login"] = self.auto_login.state
        result["recovering"] = self._recovering
        result["persistence_error"] = self._persistence_error
        return result

    def _publish(self):
        username = self.config.username.strip() or self.auth.authenticated_username
        if not username:
            raise SessionRepositoryError("无法确定会话所属用户")
        if self.auth.authenticated_username and self.config.username and self.auth.authenticated_username.casefold() != username.casefold():
            raise SessionRepositoryError("登录身份与配置账号不一致")
        credentials, token = self.store.snapshot(), self.auth.api_token()
        fingerprint = hashlib.sha256(json.dumps([credentials.cookies, credentials.tokens, token], sort_keys=True).encode()).hexdigest()
        if fingerprint != self._published_fingerprint:
            saved = self.repository.save(username, credentials, token)
            self._published_fingerprint, self._published_revision = fingerprint, saved.revision
        self._persistence_error = False

    def _restore_snapshot(self, saved):
        self.store.credentials = deepcopy(saved.credentials)
        self.store.state = "verified"

    def _login_tick(self):
        if self.auth.authentication_rejected and self._published_revision and self.requests.username:
            self.repository.delete(self.requests.username, self._published_revision)
            self._published_fingerprint = self._published_revision = None
            self.auth.authentication_rejected = False
        self.auth.collect()
        if self.store.state == "verified":
            self._publish()
            if self.config.auto_login_enabled:
                self.auto_login.state = "completed"
        else:
            self.auto_login.tick()

    def _renew_login(self):
        """在原工具调用内恢复登录；等待验证码期间仍处理状态、关闭等控制命令。"""
        self._clear()
        self._recovering = True
        progress_key = "login-" + uuid4().hex
        self._emit_progress(progress_key, "正在恢复 OA 登录")
        last_progress = None
        try:
            self.store.state = "waiting_login"
            self.browser.open(self.auth.on_request, self.auth.on_response)
            deadline = time.monotonic() + self.config.relogin_timeout
            last_check = 0.0
            while time.monotonic() < deadline:
                if time.monotonic() - last_check >= 0.5:
                    self._login_tick()
                    last_check = time.monotonic()
                if self.store.state == "verified":
                    self._emit_progress(progress_key, "OA 登录已恢复，继续原任务", "complete")
                    return
                progress_state = self.auto_login.state
                if progress_state != last_progress:
                    text = {
                        "needs_verification": "等待短信、扫码或 MFA 验证，请在浏览器完成",
                        "submitted": "已提交登录，等待 OA 验证",
                    }.get(progress_state, "等待 OA 登录，请在浏览器完成验证")
                    self._emit_progress(progress_key, text)
                    last_progress = progress_state
                if self.auto_login.state == "failed":
                    raise LoginRecoveryError("登录失败，停止自动重试")
                # Future 对应的原业务任务一直保留，成功后由执行器继续该请求。
                for _ in range(100):
                    try:
                        action, future, payload = self._commands.get_nowait()
                    except queue.Empty:
                        break
                    if action == "status":
                        future.set_result(self._status("正在恢复登录，验证完成后会继续原任务。"))
                    elif action == "credentials":
                        future.set_result(self.store.snapshot())
                    elif action in {"clear", "stop"}:
                        self.attendance.clear()
                        self._stopping = action == "stop"
                        try:
                            if action == "clear" and self.config.username.strip():
                                self.repository.delete(self.config.username.strip())
                        except SessionRepositoryError:
                            self._clear()
                            future.set_exception(RuntimeError("本地已关闭，但数据库会话删除失败"))
                            raise LoginRecoveryError("取消时数据库不可用") from None
                        self._clear()
                        future.set_result(self._status("已取消登录恢复。"))
                        raise LoginRecoveryError("用户取消")
                    elif action == "open":
                        future.set_result(self._status("正在等待登录完成，无需重复发起任务。"))
                    else:
                        self._deferred.append((action, future, payload))
                if not self.browser.pump_events():
                    raise LoginRecoveryError("登录窗口已关闭")
            raise LoginRecoveryError("登录等待超时")
        except SessionRepositoryError as error:
            self._emit_progress(progress_key, "登录恢复失败，请检查数据库配置或权限", "error")
            self._persistence_error = True
            self._persistence_message = error.public_message
            self.store.reset("error")
            raise
        except Exception:
            self._emit_progress(progress_key, "登录恢复未完成或已取消，请查看回复", "error")
            raise LoginRecoveryError("登录恢复未完成") from None
        finally:
            self._recovering = False
            for command in self._deferred:
                if self._stopping:
                    command[1].set_exception(LoginRecoveryError("会话已关闭"))
                else:
                    self._commands.put(command)
            self._deferred.clear()

    def _invalidate(self):
        self._clear()
        self.store.state = "expired"

    def _clear(self):
        # 关闭浏览器可能触发待处理回调，因此最后清理认证数据，防止凭据被重新写入。
        self.browser.close()
        self.auth.clear()
        self.auto_login.reset()
        self._published_fingerprint = self._published_revision = None
        self._persistence_error = False

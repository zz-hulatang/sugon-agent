"""处理 OA 认证规则、登录用户响应校验，以及 Cookie 和 Token 的采集。"""
import json
import re
import time
from typing import Any
from urllib.parse import unquote, urlsplit

from oa_agent.clients.browser import BrowserClient
from oa_agent.config import LoginConfig
from oa_agent.schemas.auth import Credentials
from oa_agent.sessions.store import CredentialStore


def normalize(name: str) -> str:
    """统一字段名写法，使 access_token、access-token 和 accessToken 能匹配。"""
    return re.sub(r"[-_]", "", name).lower()


def find_tokens(value: Any, keys: tuple[str, ...], prefix: str = "", depth: int = 0) -> dict[str, str]:
    """递归提取配置中的 Token 字段，结果键保留来源路径以区分同名凭据。"""
    # 限制嵌套层数，也能中断 Python 测试数据中的循环引用。
    if depth > 8:
        return {}
    if isinstance(value, str):
        # 浏览器存储经常把整个登录对象编码成 JSON 字符串，需要继续解析其内部字段。
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    found = {}
    if isinstance(value, dict):
        names = {normalize(k) for k in keys}
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if normalize(str(key)) in names and isinstance(item, str) and item:
                found[path] = item
            else:
                found.update(find_tokens(item, keys, path, depth + 1))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.update(find_tokens(item, keys, f"{prefix}[{index}]", depth + 1))
    return found


class AuthService:
    """在会话工作线程内执行认证逻辑，采集结果写入当前用户的 CredentialStore。"""

    def __init__(self, config: LoginConfig, browser: BrowserClient, store: CredentialStore):
        self.config = config
        self.browser = browser
        self.store = store
        self._headers: dict[str, str] = {}
        self._authenticated_token = ""
        self.authenticated_username = ""
        self.authentication_rejected = False

    def clear(self) -> None:
        """同时清除候选认证头、已验证 Token 和已采集凭据。"""
        self._headers.clear()
        self._authenticated_token = ""
        self.authenticated_username = ""
        self.authentication_rejected = False
        self.store.reset()

    def api_token(self) -> str:
        """只返回已采集快照中的业务 Token，供可信 OA 客户端设置 token 请求头。"""
        if not self.store.credentials.verified:
            return ""
        tokens = self.store.credentials.tokens
        # 优先使用已通过登录用户接口确认过的 Token，不任取其他来源的认证值。
        if self._authenticated_token and self._authenticated_token in tokens.values():
            return self._authenticated_token
        cookie_tokens = {value for key, value in tokens.items()
                         if key.startswith("cookie:") and key.endswith(":DP_Token") and value}
        if len(cookie_tokens) == 1:
            return cookie_tokens.pop()
        return tokens.get("header:token", "")

    def on_request(self, request):
        """仅记录允许主机上的候选认证头；发现 Token 本身不表示登录成功。"""
        if self.config.accepts_url(request.url):
            self._remember_headers(request.all_headers())

    def on_response(self, response):
        """监听 OA 页面自身的登录用户请求，不为检测额外发送请求。"""
        if self.config.accepts_url(response.url):
            self._remember_headers(response.headers)
        if response.url != self.config.user_info_url:
            return
        # 一旦接口明确拒绝认证，立即使当前快照失效，恢复等待登录。
        if response.status in {401, 403}:
            self.authentication_rejected = True
            self._authenticated_token = ""
            self.store.credentials = Credentials()
            self.store.state = "waiting_login"
            self._headers.clear()
            return
        try:
            body = response.json() if response.status == 200 else None
            if not isinstance(body, dict):
                return
            if "code" in body and body["code"] != 200:
                self._authenticated_token = ""
                return
            # 兼容公开前端使用的两种返回结构：data 中的身份，或直接返回身份字段。
            profile = body.get("data") if body.get("code") == 200 else body
            if not isinstance(profile, dict) or not any(
                isinstance(profile.get(key), str) and profile[key].strip()
                for key in ("uname", "USRID")
            ):
                self._authenticated_token = ""
                return
            headers = response.request.all_headers()
            # 绑定这次身份查询使用的 Token，采集时还要与当前页面 Cookie 进行比对。
            self._authenticated_token = headers.get("token", "")
            self.authenticated_username = profile.get("uname") or profile.get("USRID") or ""
            self.authentication_rejected = False
        except Exception:
            self._authenticated_token = ""

    def _remember_headers(self, headers):
        names = {normalize(k) for k in self.config.token_keys}
        for key, value in headers.items():
            if normalize(key) in names and value:
                self._headers[f"header:{key.lower()}"] = value

    def collect(self):
        """校验登录状态后重建内存快照；任何未通过的检查都不会保留旧凭据。"""
        self.store.credentials = Credentials()
        self.store.state = "waiting_login" if self.browser.context else "closed"
        if not self.browser.context:
            return self.store.status("请先打开 OA 并登录。")
        pages = [p for p in self.browser.context.pages if self.config.accepts_url(p.url)]
        if not pages:
            return self.store.status("尚未返回允许的 OA 地址，请完成登录；SSO 域名需由管理员配置。")
        # SSO 可能新开标签页，优先检查最新的允许域名页面。
        page = pages[-1]
        if any(frame.locator('input[type="password"]:visible').count() for frame in page.frames
               if self.config.accepts_url(frame.url)):
            return self.store.status("页面仍显示密码输入框，请先完成登录。")
        # 显式配置 URL 和元素规则时，两项均需通过；未配置时走下方接口校验。
        if self.config.success_url_regex and not re.search(self.config.success_url_regex, page.url):
            return self.store.status("当前页面尚未匹配配置的登录成功地址。")
        if self.config.success_selector and not page.locator(self.config.success_selector).first.is_visible():
            return self.store.status("尚未检测到配置的登录成功元素。")
        cookies = [c for c in self.browser.context.cookies() if self.config.accepts_cookie(c)]
        if not (self.config.success_selector or self.config.success_url_regex):
            # 不能只依据 Cookie 存在、缓存用户资料或 URL 改变判断登录成功。
            # 仅比对当前页面可用的 Cookie，避免拿其他域名或路径下的 Token 误判。
            applicable = self.browser.context.cookies([page.url])
            if not self._authenticated_token or not any(
                c["name"] == "DP_Token" and unquote(c["value"]) == self._authenticated_token
                for c in applicable
            ):
                return self.store.status("等待 OA 登录用户接口返回有效身份及匹配的 DP_Token。")
        tokens = dict(self._headers)
        for tab in pages:
            for frame in tab.frames:
                if not self.config.accepts_url(frame.url):
                    continue
                # Playwright 的通用存储快照不包含 sessionStorage，因此显式读取两类存储。
                storage = frame.evaluate("""() => ({
                    local: Object.fromEntries(Object.entries(localStorage)),
                    session: Object.fromEntries(Object.entries(sessionStorage))
                })""")
                origin = urlsplit(frame.url)
                tokens.update(find_tokens(
                    storage, self.config.token_keys, f"{origin.scheme}://{origin.netloc}"
                ))
        for cookie in cookies:
            if normalize(cookie["name"]) in {normalize(k) for k in self.config.token_keys}:
                # OA 前端对 DP_Token 使用 URL 解码；Cookie 列表仍保留原值及作用域属性。
                value = unquote(cookie["value"]) if cookie["name"] == "DP_Token" else cookie["value"]
                tokens[f"cookie:{cookie['domain']}:{cookie['path']}:{cookie['name']}"] = value
        if not cookies and not tokens:
            return self.store.status("未采集到 Cookie 或 Token，请检查登录结果及 Token 字段配置。")
        # captured_at 是用于展示/业务记录的 Unix 时间；过期计时由会话线程单独负责。
        self.store.credentials = Credentials(cookies, tokens, time.time(), True)
        self.store.state = "verified"
        return self.store.status("已检测到登录成功，并自动采集会话到本地内存。")

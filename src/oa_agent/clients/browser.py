"""封装浏览器操作；所有方法都应由所属 LoginSession 的工作线程调用。"""
from pathlib import Path
from contextlib import contextmanager

from oa_agent.config import LoginConfig


class BrowserClient:
    """管理浏览器及临时上下文，认证判断由 AuthService 负责。"""

    def __init__(self, config: LoginConfig):
        self.config = config
        self.context = None
        self._browser = None
        self._playwright = None

    def _ensure_driver(self):
        if self._playwright is None:
            from playwright.sync_api import sync_playwright
            self._playwright = sync_playwright().start()

    @contextmanager
    def request_context(self, cookies: list[dict]):
        """每次业务请求只加载数据库 Cookie，不混入浏览器内存中的其他认证状态。"""
        self._ensure_driver()
        request = self._playwright.request.new_context(storage_state={"cookies": cookies, "origins": []})
        try:
            yield request
        finally:
            request.dispose()

    @contextmanager
    def business_page(self, cookies: list[dict]):
        """提交表单使用隔离页面，只加载数据库认证，不复用登录页的内存状态。"""
        self._ensure_driver()
        channel = self.config.browser_channel or (
            "" if Path(self._playwright.chromium.executable_path).is_file() else "chrome")
        browser = self._playwright.chromium.launch(headless=True, timeout=15000, channel=channel or None)
        try:
            context = browser.new_context(storage_state={"cookies": cookies, "origins": []})
            context.set_default_timeout(15000)
            yield context.new_page()
        finally:
            browser.close()

    def open(self, on_request, on_response) -> None:
        """打开独立浏览器，并在页面请求发出前注册认证事件回调。"""
        if self.context:
            self.context.pages[-1].bring_to_front()
            return
        try:
            self._ensure_driver()
            # 优先显式指定的浏览器，其次已安装的 Chromium，最后使用系统 Chrome。
            channel = self.config.browser_channel or (
                "" if Path(self._playwright.chromium.executable_path).is_file() else "chrome"
            )
            self._browser = self._playwright.chromium.launch(
                headless=False, timeout=15000, channel=channel or None
            )
            # 使用临时上下文隔离用户会话，不读取系统 Chrome 已有的登录资料。
            self.context = self._browser.new_context()
            self.context.on("request", on_request)
            self.context.on("response", on_response)
            page = self.context.new_page()
            # 配置凭据时直接进入已核实的登录页，避免等待首页的异步跳转。
            target = self.config.login_url if self.config.auto_login_enabled else self.config.url
            page.goto(target, wait_until="domcontentloaded", timeout=25000)
        except Exception:
            self.close()
            raise

    def pump_events(self) -> bool:
        """短暂让出控制权分发 Playwright 事件；无页面时返回 False。"""
        if not self.context or not self.context.pages:
            return False
        # 同步 Playwright 需要持续分发事件，才能在等待用户登录时收到响应回调。
        self.context.pages[0].wait_for_timeout(50)
        return True

    def close(self) -> None:
        """释放浏览器和驱动；用户已手动关窗时也继续清理剩余资源。"""
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        finally:
            self.context = self._browser = None
            if self._playwright:
                try:
                    self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None

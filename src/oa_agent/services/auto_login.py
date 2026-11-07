"""在已核实的登录页执行一次账号密码登录，验证码与 MFA 交给用户。"""
from oa_agent.clients.browser import BrowserClient
from oa_agent.config import LoginConfig


class AutoLoginService:
    def __init__(self, config: LoginConfig, browser: BrowserClient):
        self.config = config
        self.browser = browser
        self.reset()

    def reset(self) -> None:
        self.state = "waiting_form" if self.config.auto_login_enabled else "disabled"
        self._attempted = False

    def tick(self) -> None:
        """由会话线程定期调用；先等待表单就绪，每次浏览器会话至多自动提交一次。"""
        if not self.config.auto_login_enabled or not self.browser.context:
            return
        try:
            for page in reversed(self.browser.context.pages):
                for frame in page.frames:
                    # 不向任意 OA 页面或第三方 iframe 填写本机保存的密码。
                    if not self.config.accepts_login_url(frame.url):
                        continue
                    if frame.locator(self.config.verification_selector).count():
                        self.state = "needs_verification"
                        return
                    if frame.locator(self.config.login_error_selector).count():
                        self.state = "failed"
                        self._attempted = True
                        return
                    if self._attempted:
                        return
                    username = frame.locator(self.config.username_selector)
                    password = frame.locator(self.config.password_selector)
                    submit = frame.locator(self.config.submit_selector)
                    if not all(locator.count() == 1 for locator in (username, password, submit)):
                        continue
                    if not (username.is_visible() and username.is_editable()
                            and password.is_visible() and password.is_editable()
                            and submit.is_visible() and submit.is_enabled()):
                        continue
                    # 先标记尝试，避免失败后后台轮询反复提交，触发账号锁定。
                    self._attempted = True
                    username.fill(self.config.username, timeout=2000)
                    password.fill(self.config.password, timeout=2000)
                    self.state = "submitted"
                    if submit.get_attribute("type") == "range":
                        # 当前 OA 的普通 range 控件在 change 事件中触发登录；使用真实键盘操作。
                        submit.press("End", timeout=2000)
                    else:
                        submit.click(timeout=2000)
                    return
        except Exception:
            # 定位器异常可能包含输入值；不向界面、模型或日志传递原始异常。
            if self._attempted:
                self.state = "failed"
            # 提交前的页面跳转可在下一轮重试；提交后的异常一律停止自动重试。

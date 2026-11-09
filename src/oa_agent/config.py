"""集中加载并校验配置，供界面、Agent 和登录服务共同使用。"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class LoginConfig:
    """OA 登录规则；指定页面规则时优先使用，否则监听登录用户接口。"""

    url: str = "https://soa.com.cn/"
    allowed_hosts: tuple[str, ...] = ("soa.com.cn", "sio.soa.com.cn", "msp.soa.com.cn")
    token_keys: tuple[str, ...] = (
        "DP_Token", "token", "access_token", "auth_token", "id_token", "authorization",
        "x-auth-token", "x-access-token",
    )
    success_selector: str = ""
    success_url_regex: str = ""
    idle_seconds: int = 1800
    browser_channel: str = ""
    user_info_url: str = "https://msp.soa.com.cn/api/sap/getLoginUser"
    # 登录凭据只由本机后端读取，不注入工具参数、模型提示词或配置 repr；查询账号可由用户另行指定。
    username: str = field(default="", repr=False)
    password: str = field(default="", repr=False)
    login_url: str = "https://soa.com.cn/sioLogin/"
    username_selector: str = "#username"
    password_selector: str = "#password"
    submit_selector: str = "#login-slider"
    verification_selector: str = '#sms-panel:visible, input[autocomplete="one-time-code"]:visible'
    login_error_selector: str = "#login-error:visible"
    relogin_timeout: int = 180

    def __post_init__(self):
        # 启动时暴露错误配置，避免后台轮询反复失败而界面一直等待登录。
        if self.idle_seconds < 1:
            raise ValueError("空闲时间必须大于零")
        if self.relogin_timeout < 1:
            raise ValueError("重新登录等待时间必须大于零")
        if self.success_url_regex:
            re.compile(self.success_url_regex)
        if not self.accepts_url(self.user_info_url):
            raise ValueError("用户信息接口必须位于允许的 HTTPS 主机")
        if bool(self.username) != bool(self.password):
            raise ValueError("OA_USERNAME 和 OA_PASSWORD 必须同时配置，或同时留空")
        if not self.accepts_url(self.login_url):
            raise ValueError("登录页必须位于允许的 HTTPS 主机")
        if not all((self.username_selector, self.password_selector, self.submit_selector)):
            raise ValueError("登录表单选择器不能为空")

    @property
    def auto_login_enabled(self) -> bool:
        """账号密码均配置时启用自动登录；都未配置时保留手动登录。"""
        return bool(self.username and self.password)

    def accepts_login_url(self, url: str) -> bool:
        """仅在配置登录页的同源、同路径填写凭据，忽略登录回跳查询参数。"""
        actual, expected = urlsplit(url), urlsplit(self.login_url)
        return (
            actual.scheme == expected.scheme == "https"
            and actual.hostname == expected.hostname
            and actual.port == expected.port
            and actual.path.rstrip("/") == expected.path.rstrip("/")
        )

    @classmethod
    def from_env(cls) -> LoginConfig:
        """读取 OA_* 变量，并保留默认可信域名、去除重复主机名。"""
        defaults = cls()
        return cls(
            allowed_hosts=tuple(dict.fromkeys((
                *defaults.allowed_hosts,
                *(h.strip().lower() for h in os.getenv("OA_EXTRA_HOSTS", "").split(",") if h.strip()),
            ))),
            token_keys=tuple(k.strip() for k in os.getenv(
                "OA_TOKEN_KEYS", ",".join(defaults.token_keys)
            ).split(",") if k.strip()),
            success_selector=os.getenv("OA_SUCCESS_SELECTOR", ""),
            success_url_regex=os.getenv("OA_SUCCESS_URL_REGEX", ""),
            idle_seconds=int(os.getenv("OA_IDLE_SECONDS", "1800")),
            browser_channel=os.getenv("OA_BROWSER_CHANNEL", ""),
            user_info_url=os.getenv("OA_USER_INFO_URL") or defaults.user_info_url,
            username=os.getenv("OA_USERNAME", ""),
            password=os.getenv("OA_PASSWORD", ""),
            login_url=os.getenv("OA_LOGIN_URL") or defaults.login_url,
            username_selector=os.getenv("OA_USERNAME_SELECTOR") or defaults.username_selector,
            password_selector=os.getenv("OA_PASSWORD_SELECTOR") or defaults.password_selector,
            submit_selector=os.getenv("OA_SUBMIT_SELECTOR") or defaults.submit_selector,
            verification_selector=os.getenv("OA_VERIFICATION_SELECTOR") or defaults.verification_selector,
            login_error_selector=os.getenv("OA_LOGIN_ERROR_SELECTOR") or defaults.login_error_selector,
            relogin_timeout=int(os.getenv("OA_RELOGIN_TIMEOUT", "180")),
        )

    def accepts_url(self, url: str) -> bool:
        """只允许明确列出的 HTTPS 主机，不自动信任其所有子域名。"""
        parsed = urlsplit(url)
        return parsed.scheme == "https" and parsed.hostname in self.allowed_hosts

    def accepts_cookie(self, cookie: dict) -> bool:
        """保留可作用于允许主机的 Cookie，包括 .soa.com.cn 这样的父域。"""
        domain = cookie.get("domain", "").lstrip(".").lower()
        return bool(domain) and any(
            host == domain or host.endswith("." + domain) for host in self.allowed_hosts
        )


@dataclass(frozen=True, repr=False)
class ModelConfig:
    """模型连接参数；关闭自动 repr，避免打印配置时暴露 API Key。"""

    api_key: str = ""
    model: str = ""
    base_url: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.model)


@dataclass(frozen=True)
class MySQLConfig:
    """MySQL 连接与认证数据加密配置，默认不把密码和密钥打印到 repr。"""

    host: str = "localhost"
    port: int = 3306
    database: str = "oa_agent"
    user: str = ""
    password: str = field(default="", repr=False)
    encryption_key: str = field(default="", repr=False)
    ssl_ca: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.user and self.encryption_key)

    @classmethod
    def from_env(cls):
        return cls(host=os.getenv("MYSQL_HOST", "localhost"), port=int(os.getenv("MYSQL_PORT", "3306")),
                   database=os.getenv("MYSQL_DATABASE", "oa_agent"), user=os.getenv("MYSQL_USER", ""),
                   password=os.getenv("MYSQL_PASSWORD", ""), encryption_key=os.getenv("SESSION_ENCRYPTION_KEY", ""),
                   ssl_ca=os.getenv("MYSQL_SSL_CA", ""))


@dataclass(frozen=True)
class AppSettings:
    """应用配置集合；登录功能可以在未配置模型时独立使用。"""

    login: LoginConfig
    model: ModelConfig
    database: MySQLConfig = field(default_factory=MySQLConfig)


def load_settings(env_file: Path | None = None) -> AppSettings:
    """加载指定 .env；已有进程环境变量优先于文件中的同名配置。"""
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=env_file)
    # 与 README 中的安装命令一致：默认使用当前 Python 环境内的浏览器。
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")
    return AppSettings(
        login=LoginConfig.from_env(),
        database=MySQLConfig.from_env(),
        model=ModelConfig(
            api_key=os.getenv("OPENAI_API_KEY", ""),
            model=os.getenv("OPENAI_MODEL", ""),
            base_url=os.getenv("OPENAI_BASE_URL", ""),
        ),
    )

"""用真实 Chrome 和本地模拟响应验证自动采集，不访问真实 OA 或用户账号。"""
import json
import time
from unittest.mock import patch
from playwright.sync_api import sync_playwright
from oa_agent.config import LoginConfig
from oa_agent.sessions.session import LoginSession
from oa_agent.sessions.repository import InMemorySessionRepository

original_factory = sync_playwright
from pathlib import Path

html = (Path(__file__).resolve().parents[1] / "fixtures" / "login.html").read_text()

def factory():
    manager = original_factory()
    original_start = manager.start
    def start():
        pw = original_start()
        original_launch = pw.chromium.launch
        def launch(**kwargs):
            kwargs['headless'] = True
            browser = original_launch(**kwargs)
            original_new_context = browser.new_context
            def new_context(*args, **kwargs):
                context = original_new_context(*args, **kwargs)
                def handle(route):
                    # 所有请求都经过路由；只返回测试页面/身份，其他地址一律阻止。
                    if route.request.url == 'https://soa.com.cn/':
                        route.fulfill(status=200, content_type='text/html', body=html,
                                      headers={'Set-Cookie': 'OA_SESSION=smoke-session; Secure; HttpOnly; Path=/'})
                    elif route.request.url == 'https://msp.soa.com.cn/api/sap/getLoginUser':
                        route.fulfill(status=200, content_type='application/json',
                                      body=json.dumps({'code': 200, 'data': {'uname': 'smoke-user'}}),
                                      headers={'Access-Control-Allow-Origin': 'https://soa.com.cn',
                                               'Access-Control-Allow-Headers': 'Token'})
                    else:
                        route.abort()
                context.route('**/*', handle)
                return context
            browser.new_context = new_context
            return browser
        pw.chromium.launch = launch
        return pw
    manager.start = start
    return manager

with patch('playwright.sync_api.sync_playwright', factory):
    session = LoginSession(LoginConfig(browser_channel='chrome'), repository=InMemorySessionRepository())
    try:
        assert session.call('open')['state'] == 'waiting_login'
        # 不发送 UI 事件或 collect 命令，验证后台能独立完成延迟登录后的采集。
        time.sleep(3.5)
        result = session.call('status')
        assert result['state'] == 'verified', result
        assert 'smoke' not in json.dumps(result)
        credentials = session.credentials()
        assert any(c['name'] == 'OA_SESSION' and c['httpOnly'] for c in credentials.cookies)
        assert credentials.tokens['cookie:soa.com.cn:/:DP_Token'] == 'smoke/token'
        assert 'local-smoke-token' in credentials.tokens.values()
        assert credentials.tokens['header:token'] == 'smoke/token'
        assert session.call('clear')['cookie_count'] == 0
        assert session.credentials().tokens == {}
        print('真实 Chrome 自动登录检测通过：延迟登录后无额外操作即可采集 HttpOnly Cookie、DP_Token、localStorage 和请求头。')
    finally:
        session.close()

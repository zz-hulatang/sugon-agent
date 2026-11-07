"""自动登录的提交次数、可信页面范围和人工验证边界。"""
import unittest
from unittest.mock import Mock

from oa_agent.config import LoginConfig
from oa_agent.services.auto_login import AutoLoginService


def make_service(**kwargs):
    config = LoginConfig(username="dummy-user", password="dummy-password", **kwargs)
    frame = Mock(url=config.login_url)
    selectors = {}
    for selector in (config.username_selector, config.password_selector, config.submit_selector):
        locator = Mock()
        locator.count.return_value = 1
        locator.is_visible.return_value = True
        locator.is_editable.return_value = True
        locator.is_enabled.return_value = True
        selectors[selector] = locator
    for selector in (config.verification_selector, config.login_error_selector):
        selectors[selector] = Mock()
        selectors[selector].count.return_value = 0
    selectors[config.submit_selector].get_attribute.return_value = "range"
    frame.locator.side_effect = selectors.__getitem__
    browser = Mock(context=Mock(pages=[Mock(frames=[frame])]))
    return AutoLoginService(config, browser), selectors, frame


class AutoLoginTests(unittest.TestCase):
    def test_slider_submission_happens_once(self):
        service, fields, _ = make_service()
        service.tick()
        service.tick()
        fields["#username"].fill.assert_called_once_with("dummy-user", timeout=2000)
        fields["#password"].fill.assert_called_once_with("dummy-password", timeout=2000)
        fields["#login-slider"].press.assert_called_once_with("End", timeout=2000)
        self.assertEqual(service.state, "submitted")

    def test_button_submission(self):
        service, fields, _ = make_service(submit_selector="#submit")
        fields["#submit"].get_attribute.return_value = "submit"
        service.tick()
        fields["#submit"].click.assert_called_once_with(timeout=2000)

    def test_disabled_slider_waits_for_initialization(self):
        service, fields, _ = make_service()
        fields["#login-slider"].is_enabled.return_value = False
        service.tick()
        fields["#password"].fill.assert_not_called()
        fields["#login-slider"].is_enabled.return_value = True
        service.tick()
        self.assertEqual(service.state, "submitted")

    def test_other_origin_and_paths_are_never_filled(self):
        service, fields, frame = make_service()
        for url in ("https://evil.example/sioLogin/", "https://soa.com.cn/retrievePass/", "https://soa.com.cn:444/sioLogin/", "http://soa.com.cn/sioLogin/"):
            frame.url = url
            service.tick()
        fields["#password"].fill.assert_not_called()
        frame.locator.assert_not_called()

    def test_secondary_verification_is_left_to_user(self):
        service, fields, _ = make_service()
        fields[service.config.verification_selector].count.return_value = 1
        service.tick()
        self.assertEqual(service.state, "needs_verification")
        fields["#password"].fill.assert_not_called()
        fields["#login-slider"].press.assert_not_called()

    def test_failed_attempt_is_not_retried(self):
        service, fields, _ = make_service()
        fields["#password"].fill.side_effect = RuntimeError("dummy-password")
        service.tick()
        service.tick()
        self.assertEqual(service.state, "failed")
        self.assertEqual(fields["#password"].fill.call_count, 1)

    def test_server_error_stops_retry_even_if_error_later_disappears(self):
        service, fields, _ = make_service()
        service.tick()
        error = fields[service.config.login_error_selector]
        error.count.return_value = 1
        service.tick()
        self.assertEqual(service.state, "failed")
        error.count.return_value = 0
        service.tick()
        self.assertEqual(fields["#login-slider"].press.call_count, 1)

    def test_unconfigured_credentials_keep_manual_login(self):
        browser = Mock()
        service = AutoLoginService(LoginConfig(), browser)
        service.tick()
        self.assertEqual(service.state, "disabled")
        browser.assert_not_called()

    def test_credentials_hidden_and_pair_required(self):
        service, _, _ = make_service()
        self.assertNotIn("dummy-password", repr(service.config))
        self.assertNotIn("dummy-user", repr(service.config))
        with self.assertRaises(ValueError):
            LoginConfig(username="dummy-user")

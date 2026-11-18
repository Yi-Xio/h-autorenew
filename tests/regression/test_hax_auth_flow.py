"""调用 Hax 实际业务函数，外部网站、读码和 WARP 使用测试替身。"""

import ast
import contextlib
import io
import re
import unittest
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, Optional

from scripts.job_summary import REASON_LABELS
from scripts.public_report import WARP_FAILURE_REASONS, record_check
from scripts.web_auth import AuthenticationError
from tests.regression.fixtures import PROJECT


def business_functions() -> dict[str, Any]:
    """加载实际公开业务函数，避免启动浏览器或安装 OCR。"""
    source = ast.parse((PROJECT / "hax-renew.py").read_text())
    names = {
        "process",
        "renew",
        "_renew_get_code",
        "handoff_browser_to_warp",
        "_renew_parse_result",
        "hax_login_flow",
        "WarpError",
    }
    functions: list[ast.stmt] = [
        node
        for node in source.body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names
    ]
    namespace: dict[str, Any] = dict(
        Dict=Dict,
        Any=Any,
        Optional=Optional,
        AuthenticationError=AuthenticationError,
        REASON_LABELS=REASON_LABELS,
        record_check=record_check,
        WARP_FAILURE_REASONS=WARP_FAILURE_REASONS,
    )
    exec(
        compile(ast.Module(body=functions, type_ignores=[]), "hax-renew.py", "exec"),
        namespace,
    )
    return namespace


class HaxAuthenticationFlowTests(unittest.TestCase):
    def test_unready_telegram_prevents_hax_authorization_requests(self) -> None:
        namespace = business_functions()
        requests = []

        def unavailable(*args: object) -> None:
            raise AuthenticationError("telegram_peer_unverified")

        namespace.update(
            new_telegram_reader=unavailable,
            restore_hax=lambda *args: requests.append("hax"),
        )
        browser = SimpleNamespace(web_account={"telegram_web": {}, "hax_web": {}})
        with self.assertRaises(AuthenticationError):
            namespace["hax_login_flow"](browser, "+1-5550001234", 1)
        self.assertEqual(requests, [])

    def test_hax_restore_keeps_the_prepared_business_reader(self) -> None:
        namespace = business_functions()
        reader = object()
        browser = SimpleNamespace(web_account={"telegram_web": {}, "hax_web": {}})

        def restore(*args: object) -> None:
            self.assertIs(browser.telegram_reader, reader)

        namespace.update(new_telegram_reader=lambda *args: reader, restore_hax=restore)
        self.assertTrue(namespace["hax_login_flow"](browser, "+1-5550001234", 1))
        self.assertIs(browser.telegram_reader, reader)

    def test_current_site_captcha_rejection_has_explicit_diagnostic(self) -> None:
        namespace = business_functions()
        checks = []
        namespace.update(
            record_check=lambda *values: checks.append(values),
            _drain_alerts=lambda browser: "",
            _renew_capture_final_info=lambda *args: None,
            time=SimpleNamespace(sleep=lambda seconds: None),
        )
        tracker = SimpleNamespace(
            active=True,
            read=lambda: dict(
                status="failed", date="", source="dialog", reason="captcha"
            ),
            close=lambda: None,
        )
        result = {}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(
                namespace["_renew_parse_result"](
                    SimpleNamespace(renewal_submission=tracker), result, "2026-11-21"
                )
            )
        self.assertEqual(checks, [("captcha", "site_feedback", "rejected")])
        self.assertEqual(result["public_reason"], "captcha_failed")

    def test_current_success_feedback_is_valid_even_when_expiry_does_not_change(
        self,
    ) -> None:
        namespace = business_functions()
        namespace.update(
            _drain_alerts=lambda browser: "",
            _renew_capture_final_info=lambda browser, result, old: result.update(
                info_valid_until=old
            ),
            time=SimpleNamespace(sleep=lambda seconds: None),
        )
        tracker = SimpleNamespace(
            active=True,
            read=lambda: dict(
                status="success", date="2026-11-21", source="dialog", reason="none"
            ),
            close=lambda: None,
        )
        result = {}
        browser = SimpleNamespace(renewal_submission=tracker)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(
                namespace["_renew_parse_result"](browser, result, "2026-11-21")
            )
        self.assertTrue(result["success"])
        self.assertEqual(result["feedback_status"], "success")
        self.assertEqual(result["info_valid_until"], result["banner_new_valid_until"])

    def test_expiry_or_unrelated_alert_does_not_override_failed_current_feedback(
        self,
    ) -> None:
        for status, active in (("failed", True), ("unknown", True), ("unknown", False)):
            namespace = business_functions()
            namespace.update(
                _drain_alerts=lambda browser: "Your VPS has been renewed until November 21, 2026",
                _renew_capture_final_info=lambda browser, result, old: result.update(
                    success=True, info_valid_until="2026-12-01", message="date_changed"
                ),
                time=SimpleNamespace(sleep=lambda seconds: None),
            )
            tracker = SimpleNamespace(
                active=active,
                read=lambda: dict(
                    status=status,
                    date="",
                    source="page",
                    reason="code" if status == "failed" else "none",
                ),
                close=lambda: None,
            )
            result = {}
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertFalse(
                    namespace["_renew_parse_result"](
                        SimpleNamespace(renewal_submission=tracker),
                        result,
                        "2026-11-21",
                    )
                )
            self.assertFalse(result["success"])
            self.assertNotEqual(result["message"], "date_changed")

    def test_handoff_restores_only_hax_cookies_before_first_navigation(self) -> None:
        """出口切换保留本站会话，不把域名相似的第三方 Cookie 带入。"""
        namespace = business_functions()
        events = []
        old = SimpleNamespace(
            cookies=lambda **kwargs: [
                dict(domain=domain, name="PHPSESSID")
                for domain in (
                    "hax.co.id",
                    ".hax.co.id",
                    "evil-hax.co.id",
                    "hax.co.id.invalid",
                )
            ],
            quit=lambda: events.append("old_closed"),
        )
        page = SimpleNamespace(
            set=SimpleNamespace(
                timeouts=lambda **kwargs: None,
                cookies=lambda cookies: events.append(("cookies", cookies)),
            ),
            wait=SimpleNamespace(doc_loaded=lambda: None),
            get=lambda url: events.append(("get", url)),
            quit=lambda **kwargs: events.append("new_closed"),
        )

        class Options:
            def auto_port(self) -> "Options":
                return self

            def set_argument(self, value: str) -> None:
                pass

        namespace.update(
            ChromiumOptions=Options,
            ChromiumPage=lambda options: page,
            block_ad_domains=lambda page: None,
            install_hax_rules=lambda page: events.append("rules_registered"),
            _DPDriver=lambda browser: object(),
            HAX_BASE_URL="https://hax.co.id",
            wait_hax_navigation=lambda browser, target: None,
            time=SimpleNamespace(sleep=lambda seconds: None),
        )
        browser = SimpleNamespace(page=old)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(
                namespace["handoff_browser_to_warp"](
                    browser, "https://hax.co.id/vps-renew-code/"
                )
            )
        self.assertEqual(events[1], "rules_registered")
        self.assertEqual(events[2][0], "cookies")
        self.assertEqual(
            [cookie["domain"] for cookie in events[2][1]], ["hax.co.id", ".hax.co.id"]
        )
        self.assertEqual(events[3], ("get", "https://hax.co.id/vps-renew-code/"))

        def expired(browser: object, target: str) -> None:
            raise AuthenticationError("hax_session_invalid")

        namespace["wait_hax_navigation"] = expired
        browser.page = old
        with (
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(AuthenticationError) as error,
        ):
            namespace["handoff_browser_to_warp"](browser)
        self.assertEqual(error.exception.reason, "hax_session_invalid")
        self.assertEqual(events[-1], "new_closed")

        def failed_navigation(url: str) -> None:
            raise RuntimeError("EXAMPLE_NAVIGATION_FAILURE")

        page.get = failed_navigation
        browser.page = old
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(namespace["handoff_browser_to_warp"](browser))
        self.assertEqual(events[-1], "new_closed")

    def test_restored_authentication_continues_existing_server_renewal(self) -> None:
        namespace = business_functions()
        events = []
        server = dict(id="example-server")
        namespace.update(
            mask_phone=lambda phone: "***123",
            publish_progress=lambda stage: events.append(stage),
            hax_login_flow=lambda *args, **kwargs: events.append("restore"),
            hax_get_vps_info=lambda *args: ([server], "", None),
            safe_sid_for_filename=lambda value: value,
            renew=lambda *args, **kwargs: dict(success=True),
            time=SimpleNamespace(sleep=lambda seconds: None),
        )
        with contextlib.redirect_stdout(io.StringIO()):
            result = namespace["process"](object(), "+1-5550001234", 1)
        self.assertTrue(result["success"])
        self.assertEqual(events[:3], ["login", "restore", "reading_vps"])
        self.assertEqual(len(result["servers"]), 1)

    def test_invalid_authentication_notifies_and_never_reads_or_renews_vps(
        self,
    ) -> None:
        for reason in (
            "telegram_session_invalid",
            "hax_session_invalid",
            "access_blocked",
            "hax_page_unverified",
        ):
            namespace = business_functions()
            notifications = []

            def invalid(*args: object, **kwargs: object) -> None:
                raise AuthenticationError(reason)

            namespace.update(
                mask_phone=lambda phone: "***123",
                publish_progress=lambda stage: None,
                hax_login_flow=invalid,
                notify_login_fail=lambda *args, **kwargs: notifications.append(
                    kwargs["reason"]
                ),
            )
            result = namespace["process"](object(), "+1-5550001234", 1)
            self.assertEqual(result["public_reason"], reason)
            self.assertNotIn("public_state", result)
            self.assertEqual(result["servers"], [])
            self.assertEqual(notifications, [reason])

    def renewal_environment(self, reader: object) -> tuple[dict[str, Any], list[str]]:
        """提供原业务流程所需的外部接口，记录提交和出口切换。"""
        namespace = business_functions()
        events = []
        manager = SimpleNamespace(
            current_tag="", rotation_count=0, warp_refresh_count=0
        )

        def completed(browser: object, result: dict[str, Any], old: str) -> bool:
            result.update(
                success=True, info_valid_until="2026-12-01", message="verified"
            )
            return True

        namespace.update(
            _parse_iso_date=lambda value: datetime.strptime(value, "%Y-%m-%d").date()
            if value
            else None,
            cn_now=lambda: datetime(2026, 11, 16),
            MIN_VALID_DAYS=5,
            MAX_RENEW_RETRIES_PER_URL=5,
            MAX_WARP_RETRIES=10,
            CaptchaBlocked=RuntimeError,
            ArithmeticCaptchaUnavailable=type(
                "ArithmeticCaptchaUnavailable", (RuntimeError,), {}
            ),
            get_warp_manager=lambda: manager,
            mask_phone=lambda value: "***123",
            safe_sid_for_filename=lambda value: value,
            re=re,
            _renew_fill_form=lambda *args: events.append("form_submit"),
            _renew_navigate_to_code_page=lambda *args: events.append("code_page"),
            restart_warp=lambda *args: True,
            ensure_warp_ready=lambda: None,
            handoff_browser_to_warp=lambda *args: events.append("handoff") or True,
            dismiss_cookie_only=lambda *args: None,
            removeAds=lambda *args: None,
            solve_arithmetic_captcha=lambda *args, **kwargs: None,
            safe_screenshot=lambda *args: None,
            shot=lambda *args: "",
            _submit_code_page=lambda *args: events.append("final_submit"),
            _renew_parse_result=completed,
            notify=lambda *args, **kwargs: events.append("notify"),
            time=SimpleNamespace(sleep=lambda seconds: None),
            HAX_VPS_RENEW_CODE_URL="https://hax.co.id/vps-renew-code/",
        )
        return namespace, events

    def test_business_flow_arms_reader_before_trigger_and_survives_handoff(
        self,
    ) -> None:
        events: list[str] = []

        class Reader:
            def arm(self) -> None:
                events.append("arm")

            def wait_for_code(self, timeout: float) -> str:
                events.append("read_code")
                return "EXAMPLE_CODE_1234567890"

        reader = Reader()
        namespace, tracked = self.renewal_environment(reader)
        namespace["_renew_fill_form"] = lambda *args: events.append("form_submit")
        namespace["handoff_browser_to_warp"] = (
            lambda *args: events.append("handoff") or True
        )
        namespace["_submit_code_page"] = lambda *args: events.append("final_submit")
        browser = SimpleNamespace(
            telegram_reader=reader,
            get_page_source=lambda: "ready",
            get_title=lambda: "ready",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            result = namespace["renew"](
                browser,
                dict(id="example", valid_until="2026-11-16"),
                1,
                "+1-5550001234",
            )
        self.assertTrue(result["success"])
        self.assertEqual(
            events, ["arm", "form_submit", "handoff", "read_code", "final_submit"]
        )
        self.assertEqual(tracked[-1], "notify")

    def test_ambiguous_code_stops_without_final_submit_or_automatic_retry(self) -> None:
        reader = SimpleNamespace(arm=lambda: None)

        def ambiguous(timeout: float) -> None:
            raise AuthenticationError("renewal_code_ambiguous")

        reader.wait_for_code = ambiguous
        namespace, events = self.renewal_environment(reader)
        browser = SimpleNamespace(
            telegram_reader=reader,
            get_page_source=lambda: "ready",
            get_title=lambda: "ready",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            result = namespace["renew"](
                browser,
                dict(id="example", valid_until="2026-11-16"),
                1,
                "+1-5550001234",
            )
        self.assertFalse(result["success"])
        self.assertEqual(result["public_reason"], "renewal_code_ambiguous")
        self.assertEqual(events.count("form_submit"), 1)
        self.assertNotIn("final_submit", events)

    def test_old_authorization_and_logout_functions_are_removed(self) -> None:
        tree = ast.parse((PROJECT / "hax-renew.py").read_text())
        functions = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.ClassDef))
        }
        self.assertTrue(
            functions.isdisjoint(
                {
                    "tg_confirm_auth_sync",
                    "TGVerificationCodeExtractor",
                    "hax_click_accept",
                    "logout",
                }
            )
        )

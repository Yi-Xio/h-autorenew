"""调用 Hax 实际业务函数，外部网站、读码和 WARP 使用测试替身。"""

import ast
import contextlib
import io
import os
import unittest
from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import patch

from scripts.hax_renewal import flow, operations
from scripts.hax_renewal.retry_report import (
    RetryStep,
    format_retry_report,
    retry_count,
    retry_scope,
    start_retry,
)
from scripts.job_summary import REASON_LABELS
from scripts.public_report import publish_results, record_check
from scripts.web_auth import AuthenticationError
from tests.regression.fixtures import (
    PROJECT,
    RenewalFeedbackFixture,
    renewal_browser,
)


def business_functions() -> dict[str, Any]:
    """加载实际公开业务函数，避免启动浏览器或安装 OCR。"""
    source = ast.parse((PROJECT / "hax-renew.py").read_text())
    names = {
        "process",
        "renew",
        "handoff_browser_to_warp",
        "hax_login_flow",
        "notify_login_fail",
        "notify_no_server",
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
        REASON_HINTS={},
        RetryStep=RetryStep,
        retry_scope=retry_scope,
        retry_count=retry_count,
        start_retry=start_retry,
        format_retry_report=format_retry_report,
        HAX_TITLE="hax.co.id",
        mask=lambda value: "***",
        _tg_header=lambda *args, **kwargs: "",
        run_renewal=flow.run_renewal,
        RenewalServices=flow.RenewalServices,
        require_time=operations.require_time,
        remaining_time=operations.remaining_time,
        is_hax_url=operations.is_hax_url,
        can_reopen_hax_page=operations.can_reopen_hax_page,
        public_expiry_valid=lambda value: value,
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
        tracker = SimpleNamespace(
            active=True,
            read=lambda: dict(
                status="failed", date="", source="dialog", reason="captcha"
            ),
            close=lambda: None,
        )
        result: dict[str, Any] = {}
        browser = SimpleNamespace(
            renewal_submission=tracker,
            page=SimpleNamespace(handle_alert=lambda **kwargs: None),
        )
        with (
            contextlib.redirect_stdout(io.StringIO()),
            patch.object(flow, "record_check") as record,
            patch.object(operations, "time"),
        ):
            self.assertFalse(
                flow.collect_result(
                    browser, result, "2026-11-21", lambda *_: ([], "", None)
                )
            )
        record.assert_called_once_with("captcha", "site_feedback", "rejected")
        self.assertEqual(result["public_reason"], "captcha_failed")

    def test_current_success_feedback_is_valid_even_when_expiry_does_not_change(
        self,
    ) -> None:
        tracker = SimpleNamespace(
            active=True,
            read=lambda: dict(
                status="success", date="2026-11-21", source="dialog", reason="none"
            ),
            close=lambda: None,
        )
        result: dict[str, Any] = {"server_id": "example"}
        browser = SimpleNamespace(
            renewal_submission=tracker,
            page=SimpleNamespace(handle_alert=lambda **kwargs: None),
        )

        def read_vps_info(*_: object) -> tuple[list[dict[str, str]], str, None]:
            return [dict(id="example", valid_until="2026-11-21")], "", None

        with (
            contextlib.redirect_stdout(io.StringIO()),
            patch.object(operations, "time"),
        ):
            self.assertTrue(
                flow.collect_result(browser, result, "2026-11-21", read_vps_info)
            )
        self.assertTrue(result["success"])
        self.assertEqual(result["feedback_status"], "success")
        self.assertEqual(result["info_valid_until"], result["banner_new_valid_until"])

    def test_expiry_or_unrelated_alert_does_not_override_failed_current_feedback(
        self,
    ) -> None:
        def read_vps_info(*_: object) -> tuple[list[dict[str, str]], str, None]:
            return [dict(id="example", valid_until="2026-12-01")], "", None

        for status, active in (("failed", True), ("unknown", True), ("unknown", False)):
            alerts = iter(["Your VPS has been renewed until November 21, 2026", None])
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
            result: dict[str, Any] = {"server_id": "example"}
            browser = SimpleNamespace(
                renewal_submission=tracker,
                page=SimpleNamespace(handle_alert=lambda **kwargs: next(alerts, None)),
            )
            with (
                contextlib.redirect_stdout(io.StringIO()),
                patch.object(operations, "time"),
            ):
                self.assertFalse(
                    flow.collect_result(browser, result, "2026-11-21", read_vps_info)
                )
            self.assertFalse(result["success"])
            self.assertEqual(result["info_valid_until"], "2026-12-01")
            self.assertEqual(result["feedback_status"], status)

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
            get=lambda url, **kwargs: events.append(("get", url)),
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
            wait_hax_navigation=lambda browser, target, **kwargs: None,
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

        def expired(browser: object, target: str, **kwargs: object) -> None:
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

        for location in ("chrome-error://chromewebdata/", "https://other.invalid/"):
            with self.subTest(location=location):
                visits: list[str] = []
                target = "https://hax.co.id/vps-renew-code/"

                def navigate(url: str, **kwargs: object) -> None:
                    visits.append(url)
                    if len(visits) == 1:
                        page.url = location
                        raise OSError("EXAMPLE_NAVIGATION_FAILURE")
                    page.url = url

                def verify(browser: object, url: str, **kwargs: object) -> None:
                    if page.url != url:
                        raise AuthenticationError("hax_page_unverified")

                page.get = navigate
                namespace["wait_hax_navigation"] = verify
                browser.page = old
                with retry_scope(), contextlib.redirect_stdout(io.StringIO()):
                    if location.startswith("chrome-error"):
                        self.assertTrue(namespace["handoff_browser_to_warp"](browser, target))
                        self.assertEqual(visits, [target, target])
                        self.assertIn("续期码页面（已恢复）", format_retry_report())
                    else:
                        with self.assertRaises(AuthenticationError):
                            namespace["handoff_browser_to_warp"](browser, target)
                        self.assertEqual(visits, [target])
                        self.assertEqual(retry_count(), 0)
                        self.assertEqual(events[-1], "new_closed")

    def test_restored_authentication_continues_existing_server_renewal(self) -> None:
        namespace = business_functions()
        events = []
        server = dict(id="example-server", valid_until="2026-11-16")
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

    def renewal_environment(
        self,
        events: list[str],
        *,
        retries: int = 0,
    ) -> dict[str, Any]:
        """通过账号入口运行实际单台 VPS 流程，替换外部服务。"""
        namespace = business_functions()
        manager = SimpleNamespace(
            current_tag="", rotation_count=0, warp_refresh_count=0
        )
        clock = SimpleNamespace(sleep=lambda seconds: None, time=lambda: 0)
        self.enterContext(patch.object(operations, "time", clock))
        self.enterContext(
            patch.object(operations, "RenewalFeedback", RenewalFeedbackFixture)
        )
        namespace.update(
            MIN_VALID_DAYS=5,
            MAX_RENEW_RETRIES_PER_URL=retries,
            MAX_WARP_RETRIES=10 if retries else 1,
            get_warp_manager=lambda: manager,
            mask_phone=lambda value: "***123",
            _prepare_initial_page=lambda *args: None,
            _check_initial_form=lambda *args: None,
            restart_warp=lambda: True,
            ensure_warp_ready=lambda: None,
            handoff_browser_to_warp=lambda *args: events.append("handoff") or True,
            _prepare_warp_page=lambda *args: None,
            _prepare_final_form=lambda *args: True,
            removeAds=lambda *args: None,
            _capture_renewal_page=lambda *args: None,
            hax_get_vps_info=lambda *args: (
                [dict(id="example", valid_until="2026-11-16")],
                "",
                None,
            ),
            notify=lambda *args, **kwargs: events.append("notify"),
        )
        return namespace

    def run_example_renewal(
        self,
        namespace: dict[str, Any],
        browser: object,
        valid_until: str,
    ) -> dict[str, Any]:
        with contextlib.redirect_stdout(io.StringIO()):
            return namespace["renew"](
                browser,
                dict(id="example", valid_until=valid_until, current_date="2026-11-16"),
                1,
                "+1-5550001234",
            )

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

        namespace = self.renewal_environment(events)
        result = self.run_example_renewal(
            namespace,
            renewal_browser(Reader(), events),
            "2026-11-16",
        )
        self.assertTrue(result["success"])
        self.assertEqual(
            events,
            [
                "arm",
                "form_submit",
                "handoff",
                "read_code",
                "observe",
                "final_submit",
                "feedback",
                "notify",
            ],
        )

    def test_ambiguous_code_stops_without_final_submit_or_automatic_retry(self) -> None:
        def ambiguous(timeout: float) -> None:
            raise AuthenticationError("renewal_code_ambiguous")

        events: list[str] = []
        browser = renewal_browser(
            SimpleNamespace(arm=lambda: None, wait_for_code=ambiguous),
            events,
        )
        namespace = self.renewal_environment(events, retries=5)
        result = self.run_example_renewal(namespace, browser, "2026-11-16")
        self.assertFalse(result["success"])
        self.assertEqual(result["public_reason"], "renewal_code_ambiguous")
        self.assertEqual(events.count("form_submit"), 1)
        self.assertNotIn("final_submit", events)

    def test_valid_period_skips_requests_and_reports_result(self) -> None:
        events: list[str] = []
        namespace = self.renewal_environment(events)
        result = self.run_example_renewal(namespace, object(), "2026-11-21")
        self.assertTrue(result["skipped"])
        self.assertEqual(events, ["notify"])

    def test_force_renewal_bypasses_website_expiry_threshold(self) -> None:
        events: list[str] = []
        namespace = self.renewal_environment(events)
        namespace["MIN_VALID_DAYS"] = None
        browser = renewal_browser(
            SimpleNamespace(arm=lambda: None, wait_for_code=lambda **kwargs: "EXAMPLE_CODE"),
            events,
        )
        result = self.run_example_renewal(namespace, browser, "2099-12-31")
        self.assertTrue(result["success"])
        self.assertEqual(events.count("form_submit"), 1)
        self.assertEqual(events.count("final_submit"), 1)

    def test_final_form_preparation_failure_does_not_click(self) -> None:
        def unavailable(*args: object) -> bool:
            raise flow.CaptchaBlocked("Example site verification failure")

        events: list[str] = []
        browser = renewal_browser(
            SimpleNamespace(
                arm=lambda: None, wait_for_code=lambda **kwargs: "EXAMPLE_CODE"
            ),
            events,
        )
        namespace = self.renewal_environment(events)
        namespace["_prepare_final_form"] = unavailable
        result = self.run_example_renewal(namespace, browser, "2026-11-16")
        self.assertFalse(result["success"])
        self.assertFalse(result["submission_attempted"])
        self.assertEqual(result["public_reason"], "captcha_failed")
        self.assertEqual(events, ["form_submit", "handoff", "notify"])

    def test_validation_failure_reports_stage_and_current_observation(self) -> None:
        for service, state, page, expected_requests, detail in (
            ("_check_initial_form", "anchor_loading", "初始续期表单", 0, "控件仍未就绪"),
            ("_prepare_final_form", "anchor_missing", "最终续期码表单", 1, "仍未找到 reCAPTCHA"),
            ("_prepare_final_form", None, "最终续期码表单", 1, "站点验证未完成"),
        ):
            with self.subTest(service=service, state=state):
                events: list[str] = []
                browser = renewal_browser(
                    SimpleNamespace(
                        arm=lambda: None, wait_for_code=lambda **kwargs: "EXAMPLE_CODE"
                    ),
                    events,
                )

                def validation_failed(*args: object) -> None:
                    if state is not None:
                        record_check("captcha", "recaptcha", state)
                    raise flow.CaptchaBlocked("EXAMPLE_PRIVATE_SITE_TEXT")

                record_check("captcha", "recaptcha", "blocked")
                namespace = self.renewal_environment(events)
                namespace[service] = validation_failed
                result = self.run_example_renewal(namespace, browser, "2026-11-16")
                self.assertEqual(result["public_reason"], "captcha_failed")
                self.assertIn(page, result["detailMessage"])
                self.assertIn(detail, result["detailMessage"])
                self.assertNotIn("EXAMPLE_PRIVATE_SITE_TEXT", result["detailMessage"])
                self.assertEqual(events.count("form_submit"), expected_requests)
                self.assertNotIn("final_submit", events)

    def test_missing_feedback_after_final_click_is_unverified(self) -> None:
        events: list[str] = []
        browser = renewal_browser(
            SimpleNamespace(
                arm=lambda: None, wait_for_code=lambda **kwargs: "EXAMPLE_CODE"
            ),
            events,
        )
        browser.page.feedback = dict(
            status="unknown", date="", source="none", reason="none"
        )
        namespace = self.renewal_environment(events, retries=5)
        result = self.run_example_renewal(namespace, browser, "2026-11-16")
        self.assertFalse(result["success"])
        self.assertTrue(result["submission_attempted"])
        self.assertEqual(result["public_reason"], "renewal_unverified")
        self.assertEqual(events.count("final_submit"), 1)
        self.assertEqual(events.count("feedback"), 6)
        self.assertEqual(events.count("form_submit"), 1)

    def test_login_failure_report_only_includes_retries_from_the_current_account(self) -> None:
        namespace = business_functions()
        reports: list[str] = []

        def fail_login(*args: object, **kwargs: object) -> None:
            start_retry(RetryStep.TELEGRAM_CHAT)
            raise AuthenticationError("telegram_peer_unverified")

        namespace.update(
            hax_login_flow=fail_login,
            hax_get_vps_info=lambda *args: ([], "", None),
            mask_phone=lambda value: "***",
            publish_progress=lambda stage: None,
            send_tg_message=lambda token, chat, text: reports.append(text) or True,
        )
        options = dict(tg_token="EXAMPLE_TOKEN", tg_chat="EXAMPLE_CHAT")
        with contextlib.redirect_stdout(io.StringIO()):
            first = namespace["process"](object(), "+1-5550001234", 1, **options)
            namespace["hax_login_flow"] = lambda *args, **kwargs: True
            second = namespace["process"](object(), "+1-5550005678", 2, **options)
        self.assertFalse(first["success"])
        self.assertEqual(first["retry_count"], 1)
        self.assertEqual(second["public_state"], "no_vps")
        self.assertEqual(second["retry_count"], 0)
        self.assertEqual(len(reports), 2)
        self.assertIn("重试操作（1 次）", reports[0])
        self.assertIn("重新打开 Telegram 中的 Hax 业务会话（未恢复）", reports[0])
        self.assertIn("重试操作（0 次）", reports[1])
        self.assertNotIn("业务会话（未恢复）", reports[1])

    def test_initial_form_reopens_before_requesting_code(self) -> None:
        events: list[str] = []
        browser = renewal_browser(
            SimpleNamespace(
                arm=lambda: events.append("arm"),
                wait_for_code=lambda **kwargs: "EXAMPLE_CODE",
            ),
            events,
        )
        checks = 0

        def verify_form(*args: object) -> None:
            nonlocal checks
            checks += 1
            if checks == 1:
                raise AuthenticationError("hax_page_unverified")

        reports: list[str] = []
        namespace = self.renewal_environment(events)
        namespace["_prepare_initial_page"] = verify_form
        namespace["notify"] = lambda *args, **kwargs: reports.append(format_retry_report())
        result = self.run_example_renewal(namespace, browser, "2026-11-16")
        self.assertTrue(result["success"])
        self.assertEqual(checks, 2)
        self.assertEqual(result["retry_count"], 1)
        self.assertEqual(events.count("arm"), 1)
        self.assertEqual(events.count("form_submit"), 1)
        self.assertEqual(len(reports), 1)
        self.assertIn("重试操作（1 次）", reports[0])
        self.assertIn("重新打开初始续期表单（已恢复）", reports[0])

    def test_missing_initial_form_field_reuses_the_page_reopen_budget(self) -> None:
        for already_reopened in (False, True):
            with self.subTest(already_reopened=already_reopened):
                events: list[str] = []
                browser = renewal_browser(
                    SimpleNamespace(
                        arm=lambda: None, wait_for_code=lambda **kwargs: "EXAMPLE_CODE"
                    ),
                    events,
                )
                open_page, evaluate = browser.open, browser.execute_script
                opened: list[str] = []

                def navigate(url: str) -> None:
                    if url == operations.HAX_VPS_RENEW_URL:
                        opened.append(url)
                    open_page(url)

                def read_field(script: str, *args: object) -> object:
                    if (
                        browser.get_current_url() == operations.HAX_VPS_RENEW_URL
                        and "return input.value === value" in script
                        and (already_reopened or len(opened) == 1)
                    ):
                        return False
                    return evaluate(script, *args)

                def verify_form(*args: object) -> None:
                    if already_reopened and len(opened) == 1:
                        raise AuthenticationError("hax_page_unverified")

                browser.open = navigate
                browser.execute_script = read_field
                namespace = self.renewal_environment(events)
                namespace["_prepare_initial_page"] = verify_form
                result = self.run_example_renewal(namespace, browser, "2026-11-16")
                self.assertEqual(len(opened), 2)
                self.assertEqual(result["success"], not already_reopened)
                self.assertEqual(events.count("form_submit"), int(not already_reopened))
                self.assertEqual(events.count("final_submit"), int(not already_reopened))

    def test_code_request_response_lost_uses_original_baseline_and_code(self) -> None:
        events: list[str] = []
        browser = renewal_browser(
            SimpleNamespace(
                arm=lambda: events.append("arm"),
                wait_for_code=lambda **kwargs: "EXAMPLE_CODE",
            ),
            events,
        )
        evaluate = browser.execute_script

        def interrupted_request(script: str, *arguments: object) -> object:
            result = evaluate(script, *arguments)
            if "btn.click()" in script:
                raise RuntimeError("EXAMPLE_DRIVER_DISCONNECT")
            return result

        browser.execute_script = interrupted_request
        namespace = self.renewal_environment(events, retries=5)
        result = self.run_example_renewal(namespace, browser, "2026-11-16")
        self.assertTrue(result["success"])
        self.assertEqual(events.count("form_submit"), 1)
        self.assertEqual(events.count("arm"), 1)
        self.assertEqual(events.count("final_submit"), 1)

    def test_explicitly_unclicked_button_can_recover_without_resending_the_code_request(self) -> None:
        for recovered in (True, False):
            with self.subTest(recovered=recovered):
                events: list[str] = []
                browser = renewal_browser(
                    SimpleNamespace(
                        arm=lambda: None, wait_for_code=lambda **kwargs: "EXAMPLE_CODE"
                    ),
                    events,
                )
                calls = 0

                def click(timeout: float) -> bool:
                    nonlocal calls
                    calls += 1
                    if calls == 2 and recovered:
                        events.append("final_submit")
                        return True
                    return False

                browser.page.ele = lambda *args, **kwargs: SimpleNamespace(click=click)
                reports: list[str] = []
                namespace = self.renewal_environment(events)
                namespace["notify"] = lambda *args, **kwargs: reports.append(format_retry_report())
                result = self.run_example_renewal(namespace, browser, "2026-11-16")
                self.assertEqual(result["success"], recovered)
                self.assertEqual(result["submission_attempted"], recovered)
                self.assertEqual(result["retry_count"], 1)
                self.assertEqual(calls, 2)
                self.assertEqual(events.count("form_submit"), 1)
                self.assertEqual(events.count("final_submit"), int(recovered))
                self.assertIn("重试操作（1 次）", reports[0])

    def test_final_button_alignment_error_retries_before_the_first_click(self) -> None:
        events: list[str] = []
        reader = SimpleNamespace(
            arm=lambda: events.append("arm"), wait_for_code=lambda **kwargs: "EXAMPLE_CODE"
        )
        browser = renewal_browser(reader, events)
        evaluate = browser.execute_script

        def align(script: str, *arguments: object) -> object:
            if "btn.scrollIntoView" in script:
                events.append("align")
                if events.count("align") == 1:
                    raise OSError("EXAMPLE_TEMPORARY_PAGE_ERROR")
            return evaluate(script, *arguments)

        browser.execute_script = align
        namespace = self.renewal_environment(events)
        result = self.run_example_renewal(namespace, browser, "2026-11-16")
        self.assertTrue(result["success"])
        self.assertEqual(result["retry_count"], 1)
        self.assertEqual(
            [event for event in events if event in {"form_submit", "align", "final_submit"}],
            ["form_submit", "align", "align", "final_submit"],
        )

    def test_public_count_adds_account_preparation_only_once(self) -> None:
        with patch("scripts.public_report._write_snapshot") as report:
            result = publish_results([dict(
                retry_count=2,
                servers=[
                    dict(success=False, public_reason="renewal_failed", retry_count=1),
                    dict(success=True, feedback_status="success", submission_attempted=True),
                ],
            )], "hax")
        self.assertEqual(result, 1)
        self.assertEqual(report.call_args.args[1]["retries"], 3)

    def test_final_click_error_never_tries_another_button(self) -> None:
        for feedback_status in ("success", "unknown"):
            with self.subTest(feedback_status=feedback_status):
                events: list[str] = []
                browser = renewal_browser(
                    SimpleNamespace(
                        arm=lambda: None, wait_for_code=lambda **kwargs: "EXAMPLE_CODE"
                    ),
                    events,
                )

                def click(timeout: float) -> None:
                    events.append("final_submit")
                    raise RuntimeError("EXAMPLE_DRIVER_DISCONNECT")

                browser.page.ele = lambda *args, **kwargs: SimpleNamespace(click=click)
                if feedback_status == "unknown":
                    browser.page.feedback = dict(
                        status="unknown", date="", source="none", reason="none"
                    )
                reports: list[str] = []
                namespace = self.renewal_environment(events, retries=5)
                namespace["notify"] = lambda *args, **kwargs: reports.append(
                    format_retry_report()
                )
                result = self.run_example_renewal(namespace, browser, "2026-11-16")
                self.assertEqual(result["success"], feedback_status == "success")
                self.assertTrue(result["submission_attempted"])
                self.assertEqual(result["feedback_status"], feedback_status)
                self.assertEqual(events.count("form_submit"), 1)
                self.assertEqual(events.count("final_submit"), 1)
                self.assertIn("重试操作（0 次）", reports[0])

    def test_warp_ip_read_failure_retries_read_without_requesting_a_new_code(self) -> None:
        events: list[str] = []
        browser = renewal_browser(
            SimpleNamespace(
                arm=lambda: events.append("arm"),
                wait_for_code=lambda **kwargs: "EXAMPLE_CODE",
            ),
            events,
        )

        def connection() -> None:
            events.append("warp_check")
            if events.count("warp_check") == 1:
                raise flow.WarpError("warp_ip_query_failed")

        namespace = self.renewal_environment(events)
        namespace["ensure_warp_ready"] = connection
        result = self.run_example_renewal(namespace, browser, "2026-11-16")
        self.assertTrue(result["success"])
        self.assertEqual(events.count("warp_check"), 2)
        self.assertEqual(events.count("form_submit"), 1)
        self.assertEqual(events.count("final_submit"), 1)
        self.assertEqual(result["warp_swaps"], 0)

    def test_no_remaining_account_time_stops_before_request(self) -> None:
        events: list[str] = []
        namespace = self.renewal_environment(events)
        browser = renewal_browser(
            SimpleNamespace(arm=lambda: events.append("arm")), events
        )
        with patch.dict(os.environ, HAX_OPERATION_DEADLINE="0"):
            result = self.run_example_renewal(namespace, browser, "2026-11-16")
        self.assertEqual(result["public_reason"], "timeout")
        self.assertFalse(result["submission_attempted"])
        self.assertEqual(events, ["notify"])

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

"""通过公开恢复接口验证已有授权、账号核对、回调和临时页面清理。"""

import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from scripts.hax_renewal.retry_report import format_retry_report, retry_scope
from scripts.hax_login import (
    hax_status,
    read_existing_authorization,
    restore_hax,
    wait_hax_navigation,
)
from scripts.web_sessions import AuthenticationError
from scripts.public_report import (
    publish_progress,
    record_check,
    record_navigation,
    validate_details,
)
from tests.regression.fixtures import (
    WidgetPage,
    authorization,
    web_account,
    navigation_sample,
)

CONFIGURATION = dict(
    bot="LoginHaxBot", callback="https://hax.co.id/authentication/", request_access=None
)


class SitePage:
    """记录浏览器的公开调用，外部响应由示例队列提供。"""

    def __init__(
        self, widget: WidgetPage | None = None, responses: list[object] | None = None
    ) -> None:
        self.widget = widget or WidgetPage()
        self.responses = (
            list(responses)
            if responses is not None
            else [dict(CONFIGURATION), True, "authenticated"]
        )
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.created_urls: list[str | None] = []
        self.browser = SimpleNamespace(new_tab=self.new_tab)

    def new_tab(self, url: str | None = None) -> WidgetPage:
        self.created_urls.append(url)
        return self.widget

    def run_cdp(self, method: str, **arguments: Any) -> dict[str, Any]:
        self.requests.append((method, arguments))
        if method == "Network.setCookies":
            return {}
        return {"result": {"value": self.responses.pop(0)}}

    def callbacks(self) -> list[str]:
        """提取发往网站的导航参数，不读取被测模块的内部状态。"""
        return [
            json.loads(arguments["expression"].rsplit(".apply(null,", 1)[1][:-1])[0]
            for method, arguments in self.requests
            if method == "Runtime.evaluate"
            and "location.assign" in arguments["expression"]
        ]


class Browser:
    """只提供站点页面导航和原有访问验证接口。"""

    def __init__(self, page: SitePage) -> None:
        self.page = page
        self.visited: list[str] = []
        self.challenge_clicks = 0

    def open(self, url: str, *, timeout: float = 30) -> None:
        self.visited.append(url)

    def click_turnstile(self) -> None:
        self.challenge_clicks += 1


class HaxLoginTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="hax-login-test-")
        self.addCleanup(temporary.cleanup)
        self.report = Path(temporary.name) / "report.json"
        report_environment = patch.dict(os.environ, PUBLIC_REPORT_FILE=str(self.report))
        report_environment.start()
        self.addCleanup(report_environment.stop)
        publish_progress("preparing")
        # 当前组用站点响应替身验证认证；页面规则由独立浏览器测试覆盖。
        waiting = patch("scripts.hax_login.install_hax_rules")
        self.waiting = waiting.start()
        self.addCleanup(waiting.stop)

    def test_existing_authorization_creates_hax_session_with_original_signature(
        self,
    ) -> None:
        payload = authorization()
        widget = WidgetPage(payload)
        page = SitePage(widget)
        browser = Browser(page)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            restore_hax(browser, web_account()["hax_web"], timeout=0)
        self.assertEqual(output.getvalue(), "")
        self.waiting.assert_called_once_with(page)
        self.assertEqual(browser.visited, ["https://hax.co.id/login"])
        self.assertTrue(widget.closed)
        injected = page.requests[0][1]["cookies"]
        self.assertEqual(
            {item["name"] for item in injected}, {"stel_ssid", "stel_token"}
        )
        self.assertTrue(
            all(item["domain"] == "oauth.telegram.org" for item in injected)
        )
        self.assertTrue(all("expires" not in item for item in injected))
        targets = page.callbacks()
        self.assertEqual(len(targets), 1)
        target = urlsplit(targets[0])
        self.assertEqual(
            (target.scheme, target.netloc, target.path),
            ("https", "hax.co.id", "/authentication/"),
        )
        fields = parse_qs(target.query)
        self.assertEqual(fields["hash"], [payload["user"]["hash"]])
        self.assertEqual(fields["id"], ["900000001"])
        self.assertEqual(fields["first_name"], ["Example"])
        outgoing = str(widget.requests)
        for forbidden in ("/auth/request", "confirmRequest", "sendMessage", "777000"):
            self.assertNotIn(forbidden, outgoing)
        self.assertEqual(len(widget.requests), 2)

    def test_loading_login_widget_can_reopen_login_page_once(self) -> None:
        page = SitePage(responses=[None, "login_required"] * 3 + [
            "login_required", dict(CONFIGURATION), True, "authenticated",
        ])
        browser = Browser(page)
        elapsed = 0.0

        def sleep(seconds: float) -> None:
            nonlocal elapsed
            elapsed += seconds

        clock = SimpleNamespace(monotonic=lambda: elapsed, sleep=sleep, time=time.time)
        with patch("scripts.hax_login.time", clock):
            restore_hax(browser, web_account()["hax_web"], timeout=2)
        self.assertEqual(browser.visited, ["https://hax.co.id/login"] * 2)
        self.assertEqual(len(page.callbacks()), 1)

    def test_lost_callback_response_checks_session_without_repeating_callback(self) -> None:
        page = SitePage()
        run_cdp = page.run_cdp

        def lose_callback(method: str, **arguments: Any) -> dict[str, Any]:
            value = run_cdp(method, **arguments)
            if "location.assign" in arguments.get("expression", ""):
                raise OSError("EXAMPLE_CALLBACK_DISCONNECT")
            return value

        page.run_cdp = lose_callback
        restore_hax(Browser(page), web_account()["hax_web"], timeout=0)
        self.assertEqual(len(page.callbacks()), 1)
        self.assertEqual(
            json.loads(self.report.read_text())["details"]["checks"]["authentication"],
            dict(step="callback", result="ready"),
        )

    def test_missing_or_wrong_account_authorization_never_calls_hax_callback(
        self,
    ) -> None:
        for payload, reason in (
            (
                dict(origin="https://hax.co.id", user=False),
                "hax_authorization_required",
            ),
            (authorization("900000002"), "hax_account_mismatch"),
        ):
            widget = WidgetPage(payload)
            page = SitePage(widget)
            with self.assertRaises(AuthenticationError) as error:
                restore_hax(Browser(page), web_account()["hax_web"], timeout=0)
            self.assertEqual(error.exception.reason, reason)
            self.assertEqual(page.callbacks(), [])
            self.assertTrue(widget.closed)

    def test_untrusted_origin_signature_age_and_extra_fields_are_rejected(self) -> None:
        samples = []
        wrong_origin = authorization()
        wrong_origin["origin"] = "https://unrelated.invalid"
        samples.append((wrong_origin, "hax_widget_unverified"))
        for change in (
            {"hash": "EXAMPLE_PRIVATE_VALUE"},
            {"auth_date": int(time.time()) - 600},
            {"auth_date": int(time.time()) + 600},
            {"auth_date": True},
            {"extra": "EXAMPLE_PRIVATE_VALUE"},
            {"first_name": "x" * 4097},
        ):
            payload = authorization()
            payload["user"].update(change)
            samples.append((payload, "hax_callback_unverified"))
        for payload, reason in samples:
            page = SitePage(WidgetPage(payload))
            with (
                self.subTest(reason=reason),
                self.assertRaises(AuthenticationError) as error,
            ):
                restore_hax(Browser(page), web_account()["hax_web"], timeout=0)
            self.assertEqual(error.exception.reason, reason)
            self.assertEqual(page.callbacks(), [])
            self.assertNotIn("EXAMPLE_PRIVATE_VALUE", str(error.exception))
            self.assertTrue(page.widget.closed)

    def test_callback_must_be_the_expected_https_hax_endpoint(self) -> None:
        for callback in (
            "http://hax.co.id/authentication/",
            "https://hax.co.id.unrelated.invalid/authentication/",
            "https://user:password@hax.co.id/authentication/",
            "https://hax.co.id:444/authentication/",
            "https://hax.co.id/other/",
            "https://hax.co.id/authentication/?redirect=external",
            "https://hax.co.id/authentication/#fragment",
        ):
            page = SitePage(responses=[{**CONFIGURATION, "callback": callback}])
            with (
                self.subTest(callback=urlsplit(callback).path),
                self.assertRaises(AuthenticationError) as error,
            ):
                restore_hax(Browser(page), web_account()["hax_web"], timeout=0)
            self.assertEqual(error.exception.reason, "hax_callback_unverified")
            self.assertEqual(page.created_urls, [])
            self.assertEqual(page.callbacks(), [])

    def test_unknown_bot_or_requested_access_stops_before_reading_credentials(
        self,
    ) -> None:
        for change in (
            {"bot": "UnrelatedBot"},
            {"request_access": []},
            {"request_access": "admin"},
            {"extra": "EXAMPLE_PRIVATE_VALUE"},
        ):
            page = SitePage(responses=[{**CONFIGURATION, **change}])
            with (
                self.subTest(field=next(iter(change))),
                self.assertRaises(AuthenticationError) as error,
            ):
                restore_hax(Browser(page), web_account()["hax_web"], timeout=0)
            self.assertEqual(error.exception.reason, "hax_widget_unverified")
            self.assertEqual(page.created_urls, [])
            self.assertEqual(page.callbacks(), [])

    def test_widget_context_or_network_failure_closes_only_temporary_page(self) -> None:
        for responses, reason in (
            ([False], "hax_widget_unverified"),
            ([True, None], "network_failed"),
        ):
            widget = WidgetPage()
            widget.responses = responses
            page = SitePage(widget)
            with self.assertRaises(AuthenticationError) as error:
                read_existing_authorization(page, "900000001", timeout=0)
            self.assertEqual(error.exception.reason, reason)
            self.assertTrue(widget.closed)
            self.assertEqual(page.callbacks(), [])
            check = json.loads(self.report.read_text())["details"]["checks"][
                "authentication"
            ]
            self.assertEqual(
                check["step"],
                "oauth_widget"
                if reason == "hax_widget_unverified"
                else "authorization",
            )
            self.assertEqual(
                check["result"],
                "missing" if reason == "hax_widget_unverified" else "unavailable",
            )

    def test_existing_authorization_widget_reopens_only_when_still_loading(self) -> None:
        for state in ("loading", "invalid"):
            with self.subTest(state=state):
                widget = WidgetPage()
                widget.responses = (
                    ["loading", "loading", "loading", True, authorization()]
                    if state == "loading"
                    else ["invalid"]
                )
                page = SitePage(widget)
                elapsed = 0.0

                def sleep(seconds: float) -> None:
                    nonlocal elapsed
                    elapsed += seconds

                clock = SimpleNamespace(monotonic=lambda: elapsed, sleep=sleep, time=time.time)
                with (
                    retry_scope(),
                    patch("scripts.hax_login.time", clock),
                ):
                    if state == "loading":
                        read_existing_authorization(page, "900000001", timeout=2)
                        self.assertEqual(len(widget.visited), 2)
                        self.assertEqual(widget.visited[0], widget.visited[1])
                        self.assertIn("Telegram 授权组件（已恢复）", format_retry_report())
                    else:
                        with self.assertRaises(AuthenticationError) as error:
                            read_existing_authorization(page, "900000001", timeout=2)
                        self.assertEqual(error.exception.reason, "hax_widget_unverified")
                        self.assertEqual(len(widget.visited), 1)
                queries = [
                    values for _, values in widget.requests if "fetch(" in values.get("expression", "")
                ]
                self.assertEqual(len(queries), int(state == "loading"))
                self.assertTrue(widget.closed)

    def test_temporarily_unreadable_widget_is_checked_again_without_reopening(self) -> None:
        widget = WidgetPage()
        original = widget.run_cdp
        calls = 0

        def read(method: str, **arguments: Any) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("EXAMPLE_TEMPORARY_READ_ERROR")
            return original(method, **arguments)

        widget.run_cdp = read
        with retry_scope(), patch("scripts.hax_login.time.sleep"):
            result = read_existing_authorization(SitePage(widget), "900000001", timeout=5)
            self.assertIn("重新读取 Hax 的 Telegram 授权组件（已恢复）", format_retry_report())
        self.assertEqual(result["id"], "900000001")
        self.assertEqual(len(widget.visited), 1)
        self.assertEqual(calls, 3)

    def test_missing_site_widget_is_distinguished_from_missing_oauth_widget(
        self,
    ) -> None:
        page = SitePage(responses=[None, "unknown"])
        with self.assertRaises(AuthenticationError) as error:
            restore_hax(Browser(page), web_account()["hax_web"], timeout=0)
        self.assertEqual(error.exception.reason, "hax_widget_unverified")
        self.assertEqual(
            json.loads(self.report.read_text())["details"]["checks"]["authentication"],
            dict(step="site_widget", result="missing"),
        )
        self.assertEqual(page.created_urls, [])

    def test_unverified_hax_page_and_access_challenge_do_not_report_login_success(
        self,
    ) -> None:
        for status, reason in (
            ("unknown", "hax_page_unverified"),
            ("login_required", "hax_page_unverified"),
            ("challenge", "access_blocked"),
            ("adblock", "adblock_detected"),
        ):
            page = SitePage(responses=[dict(CONFIGURATION), True, status])
            with self.assertRaises(AuthenticationError) as error:
                restore_hax(Browser(page), web_account()["hax_web"], timeout=0)
            self.assertEqual(error.exception.reason, reason)
            self.assertEqual(len(page.callbacks()), 1)

    def test_missing_rules_stop_before_cookie_injection_or_website_navigation(
        self,
    ) -> None:
        page = SitePage()
        browser = Browser(page)
        self.waiting.side_effect = AuthenticationError("hax_rules_unavailable")
        with self.assertRaises(AuthenticationError) as error:
            restore_hax(browser, web_account()["hax_web"], timeout=0)
        self.assertEqual(error.exception.reason, "hax_rules_unavailable")
        self.assertEqual(browser.visited, [])
        self.assertEqual(page.requests, [])

    def test_adblock_page_stops_without_reading_new_authorization(self) -> None:
        page = SitePage(responses=["adblock"])
        with self.assertRaises(AuthenticationError) as error:
            restore_hax(Browser(page), web_account()["hax_web"], timeout=0)
        self.assertEqual(error.exception.reason, "adblock_detected")
        self.assertEqual(page.created_urls, [])
        self.assertEqual(page.callbacks(), [])

    def test_page_status_rejects_unknown_protocol_results(self) -> None:
        for value in (None, [], {}, "EXAMPLE_PRIVATE_VALUE", True):
            self.assertEqual(
                hax_status(Browser(SitePage(responses=[value]))), "unknown"
            )

    def test_changed_site_context_prevents_callback_navigation(self) -> None:
        page = SitePage(responses=[dict(CONFIGURATION), False])
        with self.assertRaises(AuthenticationError) as error:
            restore_hax(Browser(page), web_account()["hax_web"], timeout=0)
        self.assertEqual(error.exception.reason, "hax_callback_unverified")
        self.assertTrue(page.widget.closed)


class HaxNavigationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="hax-navigation-test-")
        self.addCleanup(temporary.cleanup)
        self.report = Path(temporary.name) / "report.json"
        patcher = patch.dict(os.environ, PUBLIC_REPORT_FILE=str(self.report))
        patcher.start()
        self.addCleanup(patcher.stop)
        publish_progress("preparing")

    def page(self, statuses: list[str]) -> SitePage:
        responses = []
        for status in statuses:
            value = navigation_sample(status)
            value.pop("expected_route")
            value.pop("elapsed_seconds")
            responses.append(value)
        return SitePage(responses=responses)

    def test_loading_document_is_not_accepted_as_ready(self) -> None:
        browser = Browser(self.page(["loading", "unknown", "ready"]))
        wait_hax_navigation(browser, "https://hax.co.id/vps-renew-code", timeout=3)
        self.assertEqual(browser.page.responses, [])
        samples = json.loads(self.report.read_text())["details"]["navigation"]
        self.assertEqual(
            [sample["status"] for sample in samples], ["loading", "unknown", "ready"]
        )

    def test_expired_session_and_unfinished_challenge_stop_handoff(self) -> None:
        for status, reason in (
            ("login_required", "hax_session_invalid"),
            ("challenge", "access_blocked"),
            ("loading", "hax_page_unverified"),
            ("adblock", "adblock_detected"),
        ):
            with (
                self.subTest(status=status),
                self.assertRaises(AuthenticationError) as error,
            ):
                wait_hax_navigation(
                    Browser(self.page([status])),
                    "https://hax.co.id/vps-renew-code",
                    timeout=0,
                )
            self.assertEqual(error.exception.reason, reason)

    def test_navigation_rejects_a_ready_response_after_the_account_deadline(self) -> None:
        elapsed = 100.0
        limits: list[float] = []

        def read(method: str, **arguments: Any) -> dict[str, Any]:
            nonlocal elapsed
            limits.append(arguments["_timeout"])
            elapsed += 15
            return {"result": {"value": navigation_sample()}}

        def monotonic() -> float:
            return elapsed

        browser = Browser(SitePage())
        browser.page.run_cdp = read
        with (
            patch.dict(os.environ, HAX_OPERATION_DEADLINE="101"),
            patch("scripts.hax_login.time.monotonic", monotonic),
            patch("scripts.hax_renewal.timing.monotonic", monotonic),
            self.assertRaises(TimeoutError),
        ):
            wait_hax_navigation(browser, "https://hax.co.id/vps-renew-code")
        self.assertEqual(limits, [1.0])

    def test_navigation_report_is_bounded_and_rejects_page_text(self) -> None:
        record_check("authentication", "callback", "ready")
        record_check("captcha", "audio_recognition", "service_failed")
        for elapsed in range(9):
            record_navigation(dict(navigation_sample(), elapsed_seconds=elapsed))
        value = json.loads(self.report.read_text())
        self.assertEqual(len(value["details"]["navigation"]), 6)
        self.assertLess(self.report.stat().st_size, 4096)
        self.assertEqual(value["details"]["navigation"][0]["elapsed_seconds"], 3)
        for invalid in (
            dict(navigation_sample(), title="EXAMPLE_PRIVATE_VALUE"),
            dict(navigation_sample(), current_route="EXAMPLE_PRIVATE_VALUE"),
            dict(navigation_sample(), elapsed_seconds=True),
            dict(navigation_sample(), code_input="EXAMPLE_PRIVATE_VALUE"),
        ):
            with self.assertRaises(ValueError):
                record_navigation(invalid)
        self.assertEqual(json.loads(self.report.read_text()), value)

    def test_check_report_rejects_free_text_and_resets_for_next_worker(self) -> None:
        record_check("captcha", "audio_source", "url_missing")
        value = json.loads(self.report.read_text())
        for checks in (
            {"captcha": {"step": "audio_source", "result": "EXAMPLE_PRIVATE_VALUE"}},
            {"captcha": {"step": "EXAMPLE_PRIVATE_VALUE", "result": "url_missing"}},
            {
                "captcha": {
                    "step": "audio_source",
                    "result": "url_missing",
                    "answer": "EXAMPLE_PRIVATE_VALUE",
                }
            },
            {"unrelated": {"step": "audio_source", "result": "url_missing"}},
            {"captcha": {"step": [], "result": "url_missing"}},
            {"captcha": {"step": "audio_source", "result": []}},
        ):
            with self.assertRaises(ValueError):
                validate_details({**value["details"], "checks": checks})
        publish_progress("preparing")
        self.assertNotIn("checks", json.loads(self.report.read_text())["details"])

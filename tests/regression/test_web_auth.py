"""使用浏览器替身验证会话恢复、业务限定读取及新码选择，不连接 Telegram。"""

import copy
import json
import os
import tempfile
import time
import unittest
from typing import Any
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts.hax_renewal.retry_report import format_retry_report, retry_scope
from scripts.web_auth import (
    AuthenticationError,
    TelegramWeb,
    new_telegram_reader,
)
from tests.regression.fixtures import web_account
from scripts.public_report import publish_progress


class Page:
    """只提供读取和会话注入能力；任何点击、输入等操作均不可用。"""

    def __init__(self) -> None:
        self.url = "https://web.telegram.org/k/#@HaxTG_bot"
        self.value: dict[str, Any] = dict(status="ready", messages=[])
        self.commands: list[tuple[str, dict[str, Any]]] = []
        self.scripts: list[str] = []
        self.set = SimpleNamespace(timeouts=lambda **kwargs: None)

    def get(self, url: str, **kwargs: object) -> None:
        self.url = url

    def run_cdp(self, command: str, **values: Any) -> None:
        self.commands.append((command, values))

    def run_js(self, source: str, *arguments: object, timeout: float = 5) -> dict[str, Any]:
        self.scripts.append(source)
        return copy.deepcopy(self.value)

    def quit(self, **values: object) -> None:
        pass


def message(mid: int, text: str, timestamp: int | None = None) -> dict[str, str]:
    """生成带服务器时间的示例业务消息。"""
    return dict(
        id=str(mid),
        timestamp=str(timestamp if timestamp is not None else int(time.time())),
        text=text,
    )


class WebAuthenticationTests(unittest.TestCase):
    def test_unready_client_and_missing_business_chat_have_distinct_diagnostics(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="hax-web-readiness-test-") as temporary:
            report = Path(temporary) / "report.json"
            with patch.dict(os.environ, PUBLIC_REPORT_FILE=str(report)):
                for status in ("client_loading", "peer_loading"):
                    publish_progress("preparing")
                    page = Page()
                    page.value = dict(status=status, messages=[])
                    reader = TelegramWeb(page, web_account()["telegram_web"])
                    with self.assertRaises(AuthenticationError) as error:
                        reader.restore(timeout=0)
                    self.assertEqual(error.exception.reason, "telegram_peer_unverified")
                    self.assertEqual(
                        json.loads(report.read_text())["details"]["checks"][
                            "authentication"
                        ],
                        dict(step="telegram_web", result=status),
                    )

    def test_loading_client_or_business_chat_reopens_with_backoff(self) -> None:
        for state in ("client_loading", "peer_loading"):
            for outcome in ("ready", state, "wrong_account"):
                with self.subTest(state=state, outcome=outcome):
                    page = Page()
                    page.value = dict(status=state, messages=[])
                    reloaded: list[tuple[str, float]] = []
                    elapsed = 0.0

                    def run_cdp(command: str, **values: Any) -> None:
                        Page.run_cdp(page, command, **values)
                        if command == "Page.reload":
                            reloaded.append((page.url, elapsed))
                            if outcome == "wrong_account":
                                page.value["status"] = outcome

                    def sleep(seconds: float) -> None:
                        nonlocal elapsed
                        elapsed += seconds
                        if outcome == "ready" and reloaded and elapsed >= reloaded[0][1] + 65:
                            page.value["status"] = outcome

                    page.run_cdp = run_cdp
                    clock = SimpleNamespace(monotonic=lambda: elapsed, sleep=sleep)
                    reader = TelegramWeb(page, web_account()["telegram_web"])
                    with (
                        retry_scope(),
                        patch("scripts.web_auth.time", clock),
                        patch("scripts.web_auth.uniform", return_value=1),
                    ):
                        if outcome == "ready":
                            reader.restore()
                        else:
                            with self.assertRaises(AuthenticationError) as error:
                                reader.restore()
                            self.assertEqual(
                                error.exception.reason,
                                "telegram_session_invalid"
                                if outcome == "wrong_account"
                                else "telegram_peer_unverified",
                            )
                        report = format_retry_report()
                    label = (
                        "重新加载 Telegram Web"
                        if state == "client_loading"
                        else "重新打开 Telegram 中的 Hax 业务会话"
                    )
                    self.assertIn(label, report)
                    self.assertTrue(
                        all(url == "https://web.telegram.org/k/#@HaxTG_bot" for url, _ in reloaded)
                    )
                    if outcome == state:
                        self.assertEqual([when for _, when in reloaded], [45, 136, 257])
                        self.assertEqual(elapsed, 480)
                        self.assertIn("重试操作（3 次）", report)
                        self.assertIn("未恢复 3 次", report)
                    else:
                        self.assertEqual([when for _, when in reloaded], [45])
                        self.assertIn("重试操作（1 次）", report)
                    if outcome == "ready":
                        self.assertIn("已恢复），重开后观察 65 秒", report)

    def test_loading_progress_gets_time_to_finish_without_reloading(self) -> None:
        page = Page()
        page.value = dict(status="client_loading", messages=[])
        elapsed = 0.0

        def sleep(seconds: float) -> None:
            nonlocal elapsed
            elapsed += seconds
            if elapsed >= 78:
                page.value["status"] = "ready"
            elif elapsed >= 40:
                page.value["status"] = "peer_loading"

        reader = TelegramWeb(page, web_account()["telegram_web"])
        clock = SimpleNamespace(monotonic=lambda: elapsed, sleep=sleep)
        with (
            retry_scope(),
            patch("scripts.web_auth.time", clock),
            patch("scripts.web_auth.uniform", return_value=1),
        ):
            reader.restore()
            self.assertIn("重试操作（0 次）", format_retry_report())
        self.assertEqual(elapsed, 78)
        self.assertEqual([name for name, _ in page.commands], [
            "Page.addScriptToEvaluateOnNewDocument"
        ])

    def test_reload_preserves_observation_time_within_a_shortened_deadline(self) -> None:
        for timeout, expected_reload in ((105, []), (120, [15.0])):
            with self.subTest(timeout=timeout):
                page = Page()
                page.value = dict(status="peer_loading", messages=[])
                elapsed = 0.0
                navigations: list[float] = []

                def sleep(seconds: float) -> None:
                    nonlocal elapsed
                    elapsed += seconds

                def run_cdp(command: str, **values: Any) -> None:
                    if command == "Page.reload":
                        navigation_time = float(values["_timeout"])
                        navigations.append(navigation_time)
                        sleep(navigation_time)

                page.run_cdp = run_cdp
                reader = TelegramWeb(page, web_account()["telegram_web"])
                clock = SimpleNamespace(monotonic=lambda: elapsed, sleep=sleep)
                with (
                    retry_scope(),
                    patch("scripts.web_auth.time", clock),
                    patch("scripts.web_auth.uniform", return_value=1),
                    self.assertRaises(AuthenticationError) as error,
                ):
                    reader.restore(timeout=timeout)
                self.assertEqual(error.exception.reason, "telegram_peer_unverified")
                self.assertEqual(elapsed, timeout)
                self.assertEqual(navigations, expected_reload)

    def test_failed_first_navigation_reopens_only_the_original_business_page(self) -> None:
        page = Page()
        page.url = "about:blank"
        elapsed = 0.0
        navigations = []

        def navigate(url: str, **options: object) -> None:
            navigations.append((url, elapsed))
            if len(navigations) == 1:
                raise OSError("EXAMPLE_TEMPORARY_CONNECTION_FAILURE")
            page.url = url

        def sleep(seconds: float) -> None:
            nonlocal elapsed
            elapsed += seconds

        page.get = navigate
        clock = SimpleNamespace(monotonic=lambda: elapsed, sleep=sleep)
        reader = TelegramWeb(page, web_account()["telegram_web"])
        with (
            retry_scope(),
            patch("scripts.web_auth.time", clock),
            patch("scripts.web_auth.uniform", return_value=1),
        ):
            reader.restore()
            report = format_retry_report()
        self.assertEqual(
            navigations,
            [("https://web.telegram.org/k/#@HaxTG_bot", at) for at in (0.0, 45.0)],
        )
        self.assertIn("重新加载 Telegram Web（已恢复）", report)

    def test_temporary_read_failure_preserves_the_active_code_request(self) -> None:
        page = Page()
        reader = TelegramWeb(page, web_account()["telegram_web"])
        page.value["messages"] = [message(10, "")]
        reader.arm()
        now = int(time.time())
        with (
            retry_scope(),
            patch.object(page, "run_js", side_effect=[
                OSError("EXAMPLE_TEMPORARY_READ_FAILURE"),
                dict(status="ready", messages=[
                    message(10, "Renewal code is 111111", now),
                    message(11, "Renewal code is 222222", now),
                ]),
            ]),
            patch("scripts.web_auth.time.sleep"),
        ):
            self.assertEqual(reader.wait_for_code(), "222222")
            report = format_retry_report()
        self.assertIn("重试操作（1 次）", report)
        self.assertIn("重新读取 Telegram Web（已恢复）", report)
        self.assertFalse(any(command == "Page.reload" for command, _ in page.commands))

    def test_browser_response_after_deadline_cannot_restore_the_session(self) -> None:
        page = Page()
        reader = TelegramWeb(page, web_account()["telegram_web"])
        clock = Mock()
        elapsed = 0.0
        clock.monotonic.side_effect = lambda: elapsed

        def late_read(*args: object, timeout: float) -> dict[str, Any]:
            nonlocal elapsed
            self.assertLessEqual(timeout, 2)
            elapsed = 3
            return dict(status="ready", messages=[])

        with patch.object(page, "run_js", side_effect=late_read), patch("scripts.web_auth.time", clock):
            with self.assertRaises(AuthenticationError) as error:
                reader.restore(timeout=2)
        self.assertEqual(error.exception.reason, "telegram_peer_unverified")

    def test_reader_startup_failure_cleans_browser_and_preserves_fixed_reason(
        self,
    ) -> None:
        """浏览器恢复失败只报告固定原因，并关闭本次浏览器。"""
        page = Page()
        page.value = dict(status="login_required", messages=[])
        page.set = SimpleNamespace(timeouts=lambda **kwargs: None)
        closed = []
        page.quit = lambda **kwargs: closed.append(True)
        with (
            patch("scripts.web_auth.ChromiumPage", return_value=page),
            self.assertRaises(AuthenticationError) as error,
        ):
            new_telegram_reader(web_account()["telegram_web"])
        self.assertEqual(error.exception.reason, "telegram_session_invalid")
        self.assertEqual(closed, [True])
        with (
            patch(
                "scripts.web_auth.ChromiumPage",
                side_effect=RuntimeError("EXAMPLE_PRIVATE_VALUE"),
            ),
            self.assertRaises(AuthenticationError) as error,
        ):
            new_telegram_reader(web_account()["telegram_web"])
        self.assertEqual(str(error.exception), "browser_unavailable")

    def reader(self) -> tuple[TelegramWeb, Page]:
        """从公开接口建立全新会话读取器。"""
        page = Page()
        reader = TelegramWeb(page, web_account()["telegram_web"])
        reader.restore(timeout=0)
        return reader, page

    def test_restore_only_seeds_expected_origin_and_reads_business_dom(self) -> None:
        reader, page = self.reader()
        reader.arm()
        self.assertEqual(page.commands[0][0], "Page.addScriptToEvaluateOnNewDocument")
        self.assertIn(
            "location.origin === 'https://web.telegram.org'",
            page.commands[0][1]["source"],
        )
        for source in page.scripts:
            self.assertIn('data-peer-id="1967189265"', source)
            self.assertNotIn(".click(", source)
            self.assertNotIn("sendMessage", source)
            self.assertNotIn("readHistory", source)
            self.assertNotIn("document.body.innerText", source)

    def test_historical_and_pretrigger_messages_cannot_supply_code(self) -> None:
        for mid, timestamp in (
            (10, int(time.time())),
            (11, int(time.time()) - 60),
            (12, int(time.time()) - 200),
        ):
            reader, page = self.reader()
            page.value["messages"] = [message(10, "")]
            reader.arm()
            page.value["messages"] = [message(mid, "Your Code is 123456", timestamp)]
            with self.subTest(mid=mid), self.assertRaises(AuthenticationError) as error:
                reader.wait_for_code(timeout=0)
            self.assertEqual(error.exception.reason, "renewal_code_unavailable")

    def test_accepts_one_new_labeled_code_and_does_not_reuse_it(self) -> None:
        reader, page = self.reader()
        reader.arm()
        code = "FAKE_BASE64_RENEWAL_CODE_123456"
        page.value["messages"] = [message(11, "Your Code is: " + code)]
        self.assertEqual(reader.wait_for_code(timeout=0), code)
        with self.assertRaises(AuthenticationError):
            reader.wait_for_code(timeout=0)
        reader.arm()
        with self.assertRaises(AuthenticationError):
            reader.wait_for_code(timeout=0)

    def test_current_attempt_code_is_kept_while_site_preparation_takes_time(
        self,
    ) -> None:
        reader, page = self.reader()
        started = 2_000_000_000
        with patch("scripts.web_auth.time.time", return_value=started):
            reader.arm()
        code = "EXAMPLE_CURRENT_ATTEMPT_CODE_123456"
        page.value["messages"] = [message(11, "Your Code is " + code, started + 1)]
        with patch("scripts.web_auth.time.time", return_value=started + 600):
            self.assertEqual(reader.wait_for_code(timeout=0), code)

    def test_waiting_for_code_reopens_loading_chat_with_original_message_boundary(self) -> None:
        for initial_status, after_reload in (
            ("peer_loading", "ready"),
            ("peer_loading", "wrong_account"),
            ("ready", "ready"),
        ):
            with self.subTest(initial=initial_status, after=after_reload):
                reader, page = self.reader()
                sent_at = 1000
                elapsed = 0.0
                reloads: list[str] = []

                def sleep(seconds: float) -> None:
                    nonlocal elapsed
                    elapsed += seconds

                def run_cdp(command: str, **values: Any) -> None:
                    if command == "Page.reload":
                        reloads.append(page.url)
                        page.value = dict(
                            status=after_reload,
                            messages=[
                                message(10, "Your Code is 111111", sent_at),
                                message(11, "Your Code is 222222", sent_at - 1),
                                message(12, "Your Code is 333333", sent_at + 1),
                            ],
                        )

                page.run_cdp = run_cdp
                page.value["messages"] = [message(10, "", sent_at)]
                clock = SimpleNamespace(
                    monotonic=lambda: elapsed, sleep=sleep, time=lambda: sent_at + elapsed
                )
                with (
                    retry_scope(),
                    patch("scripts.web_auth.time", clock),
                    patch("scripts.web_auth.uniform", return_value=1),
                ):
                    reader.arm()
                    page.value = dict(status=initial_status, messages=[])
                    if initial_status == "peer_loading" and after_reload == "ready":
                        self.assertEqual(reader.wait_for_code(timeout=150), "333333")
                        self.assertIn("业务会话（已恢复）", format_retry_report())
                    else:
                        with self.assertRaises(AuthenticationError) as error:
                            reader.wait_for_code(timeout=150)
                        self.assertEqual(
                            error.exception.reason,
                            "telegram_session_invalid"
                            if after_reload == "wrong_account"
                            else "renewal_code_unavailable",
                        )
                        if initial_status == "ready":
                            self.assertIn("重试操作（0 次）", format_retry_report())
                self.assertEqual(
                    reloads, ["https://web.telegram.org/k/#@HaxTG_bot"]
                    if initial_status == "peer_loading" else []
                )

    def test_rejects_multiple_messages_or_codes_and_unlabeled_numbers(self) -> None:
        for items, expected in (
            (
                [
                    message(11, "Your Code is 123456"),
                    message(12, "Your Code is 654321"),
                ],
                "renewal_code_ambiguous",
            ),
            ([message(11, "Code: 123456 Code: 654321")], "renewal_code_ambiguous"),
            ([message(11, "VPS 123456 renewed 2026")], "renewal_code_unavailable"),
        ):
            reader, page = self.reader()
            reader.arm()
            page.value["messages"] = items
            with self.assertRaises(AuthenticationError) as error:
                reader.wait_for_code(timeout=0)
            self.assertEqual(error.exception.reason, expected)

    def test_wrong_origin_login_and_identity_stop_without_fallback(self) -> None:
        reader, page = self.reader()
        for status in ("login_required", "wrong_account"):
            page.value = dict(status=status, messages=[])
            with self.assertRaises(AuthenticationError) as error:
                reader.arm()
            self.assertEqual(error.exception.reason, "telegram_session_invalid")
        page.url = "https://unrelated.invalid/k/"
        with self.assertRaises(AuthenticationError) as error:
            reader.read()
        self.assertEqual(error.exception.reason, "telegram_peer_unverified")

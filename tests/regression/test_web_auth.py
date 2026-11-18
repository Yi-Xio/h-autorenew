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
from unittest.mock import patch

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

    def get(self, url: str) -> None:
        self.url = url

    def run_cdp(self, command: str, **values: Any) -> None:
        self.commands.append((command, values))

    def run_js(self, source: str, *arguments: object) -> dict[str, Any]:
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

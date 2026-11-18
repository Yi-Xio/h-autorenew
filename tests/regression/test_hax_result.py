"""使用本地示例页面验证实际 DOM 和弹窗，不连接 Hax 或 Telegram。"""

import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from scripts.hax_result import RenewalFeedback
from scripts.hax_renewal import operations
from scripts.hax_login import hax_status, wait_hax_navigation
from scripts.browser_setup import ADBLOCK_MESSAGE, has_adblock_dialog
from scripts.web_sessions import AuthenticationError

NOTICE = "Your VPS has been renewed until November 21, 2026"
FORM = '<input name="code"><button name="submit_button">Renew VPS</button>'


class FixtureHandler(BaseHTTPRequestHandler):
    """只提供内存中的示例网页，不记录请求。"""

    body = FORM + '<div id="response"></div>'

    def do_GET(self) -> None:
        content = (
            "<!doctype html><html><body>" + self.body + "</body></html>"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *arguments: object) -> None:
        pass


class FixturePage:
    """在本地页面执行生产观察脚本，仅替换它读取的站点地址。"""

    def __init__(self, page: Any, origin: str = "https://hax.co.id") -> None:
        self.page = page
        self.url = origin + "/vps-renew-code"
        self.location = json.dumps(dict(origin=origin, pathname="/vps-renew-code"))

    def run_cdp(self, method: str, **arguments: Any) -> dict[str, Any]:
        # 真实浏览器提供 DOM、事件、存储和弹窗；示例地址不发往外部站点。
        for field in ("expression", "source"):
            if field in arguments:
                script = arguments[field]
                if field == "expression":
                    script = "return (" + script.rstrip().rstrip(";") + ");"
                arguments[field] = (
                    "(function(location){" + script + "})(" + self.location + ")"
                )
        return self.page.run_cdp(method, **arguments)

    def handle_alert(self, **arguments: Any) -> object:
        return self.page.handle_alert(**arguments)


@unittest.skipUnless(
    os.environ.get("HAX_BROWSER_TESTS") == "1",
    "通过 HAX_BROWSER_TESTS=1 启用本地浏览器场景",
)
class HaxResultBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from DrissionPage import ChromiumOptions, ChromiumPage

        cls.temporary = tempfile.TemporaryDirectory(prefix="hax-result-fixture-")
        cls.addClassCleanup(cls.temporary.cleanup)
        options = ChromiumOptions().auto_port()
        options.set_tmp_path(str(Path(cls.temporary.name) / "browser"))
        options.headless()
        options.set_argument("--no-sandbox")
        options.set_argument("--disable-dev-shm-usage")
        cls.page = ChromiumPage(options)
        cls.addClassCleanup(cls.page.quit, timeout=5, force=True, del_data=True)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self) -> None:
        self.show(FORM + '<div id="response"></div>')
        self.page.run_js("sessionStorage.clear()")

    def show(self, body: str) -> None:
        FixtureHandler.body = body
        self.page.get(self.url + "/vps-renew-code")

    def tracker(self) -> RenewalFeedback:
        feedback = RenewalFeedback(FixturePage(self.page))
        self.addCleanup(feedback.close)
        return feedback

    def wait_feedback(self, feedback: RenewalFeedback, expected: str) -> dict[str, str]:
        deadline = time.monotonic() + 2
        while True:
            result = feedback.read()
            if result["status"] == expected or time.monotonic() >= deadline:
                self.assertEqual(result["status"], expected)
                return result
            time.sleep(0.05)

    def test_local_form_dispatches_one_request_and_one_final_click(self) -> None:
        self.show(
            '<input name="web_address"><input name="agreement" type="checkbox">'
            '<button name="submit_button" type="button">Renew VPS</button>'
            '<div id="response"></div><script>'
            'window.requests=0;document.querySelector("button").onclick=()=>{'
            'window.requests++;document.querySelector("#response").innerHTML='
            + json.dumps('<a href="https://hax.co.id/vps-renew-code">INPUT RENEW CODE</a>')
            + ';};</script>'
        )
        location = dict(origin="https://hax.co.id", pathname="/vps-renew")

        def execute(script: str, *arguments: object) -> object:
            return self.page.run_js(
                "return (function(location){return (function(){"
                + script + "}).apply(null," + json.dumps(arguments) + ");})("
                + json.dumps(location) + ");"
            )

        browser = SimpleNamespace(
            execute_script=execute, get_current_url=lambda: operations.HAX_VPS_RENEW_URL
        )
        operations.fill_site_address(browser)
        operations.accept_agreement(browser)
        self.assertTrue(operations.click_code_request(browser))
        self.assertEqual(operations.wait_for_code_link(browser), operations.HAX_VPS_RENEW_CODE_URL)
        self.assertFalse(operations.click_code_request(browser))
        self.assertEqual(self.page.run_js("return window.requests"), 1)

        self.show(
            FORM + '<button class="btn-primary">Fallback</button><div id="response"></div>'
            '<script>window.requests=0;for(const button of document.querySelectorAll("button")){'
            'button.onclick=()=>{window.requests++;document.querySelector("#response").innerHTML='
            + json.dumps(f'<div class="alert-success">{NOTICE}</div>') + ';};}</script>'
        )
        location["pathname"] = "/vps-renew-code"
        tracker = self.tracker()

        def locate(selector: str, timeout: float) -> object:
            button = self.page.ele(selector, timeout=timeout)
            if not button:
                return None

            def click(timeout: float) -> None:
                button.click()
                raise OSError("EXAMPLE_CLICK_DISCONNECT")

            return SimpleNamespace(click=click)

        browser.page = SimpleNamespace(ele=locate, handle_alert=self.page.handle_alert)
        browser.get_current_url = lambda: operations.HAX_VPS_RENEW_CODE_URL
        browser.renewal_submission = tracker
        operations.fill_renewal_code(browser, "EXAMPLE_CODE")
        self.assertTrue(operations.click_final_button(browser))
        self.wait_feedback(tracker, "success")
        self.assertEqual(self.page.run_js("return window.requests"), 1)

    def test_renewal_code_stays_out_of_an_unexpected_document(self) -> None:
        self.show(FORM)
        locations = (
            dict(origin="https://other.invalid", pathname="/vps-renew-code"),
            dict(origin="https://hax.co.id", pathname="/login"),
        )
        for location in locations:
            with self.subTest(location=location):
                def execute(script: str, *arguments: object) -> object:
                    return self.page.run_js(
                        "return (function(location){return (function(){"
                        + script + "}).apply(null," + json.dumps(arguments) + ");})("
                        + json.dumps(location) + ");"
                    )

                # 标签地址检查与实际填写之间，页面发生了导航。
                browser = SimpleNamespace(
                    execute_script=execute,
                    get_current_url=lambda: operations.HAX_VPS_RENEW_CODE_URL,
                )
                with self.assertRaises(AuthenticationError) as error:
                    operations.fill_renewal_code(browser, "EXAMPLE_CODE")
                self.assertEqual(error.exception.reason, "hax_page_unverified")
                self.assertEqual(
                    self.page.run_js("return document.querySelector('input').value"), ""
                )

    def test_script_text_and_existing_success_are_not_current_feedback(self) -> None:
        self.show(
            FORM
            + f'<div id="response"><div class="alert-success">{NOTICE}</div></div><script>const example={json.dumps(NOTICE)};</script>'
        )
        feedback = self.tracker()
        feedback.activate()
        self.assertEqual(feedback.read()["status"], "unknown")
        self.page.run_js("document.body.appendChild(document.createElement('aside'))")
        self.assertEqual(feedback.read()["status"], "unknown")
        self.page.run_js(
            "document.getElementById('response').appendChild(document.createElement('progress'))"
        )
        self.assertEqual(feedback.read()["status"], "unknown")
        self.page.run_js(
            "document.getElementById('response').innerHTML=arguments[0]",
            f'<div class="alert-success">{NOTICE}</div>',
        )
        result = self.wait_feedback(feedback, "success")
        self.assertEqual(result["date"], "2026-11-21")
        self.assertEqual(result["source"], "page")

    def test_hidden_success_requires_actual_display_after_submission(self) -> None:
        feedback = self.tracker()
        feedback.activate()
        self.page.run_js(
            "document.getElementById('response').innerHTML=arguments[0]",
            f'<div class="alert-success" style="display:none">{NOTICE}</div>',
        )
        self.assertEqual(feedback.read()["status"], "unknown")
        self.page.run_js(
            "document.querySelector('.alert-success').style.display='block'"
        )
        self.wait_feedback(feedback, "success")

    def test_native_alert_is_preserved_and_classified(self) -> None:
        feedback = self.tracker()
        feedback.activate()
        self.page.run_js("setTimeout(() => alert(arguments[0]), 0)", NOTICE)
        self.assertEqual(self.page.handle_alert(accept=True, timeout=2), NOTICE)
        result = self.wait_feedback(feedback, "success")
        self.assertEqual(result["source"], "dialog")
        self.assertEqual(result["date"], "2026-11-21")

    def test_explicit_failure_is_not_overridden_by_success_script(self) -> None:
        self.show(
            FORM
            + f'<div id="response"></div><script>const example={json.dumps(NOTICE)};</script>'
        )
        feedback = self.tracker()
        feedback.activate()
        self.page.run_js(
            "document.getElementById('response').innerHTML='<div class=\"alert-danger\">Verification code is wrong</div>'"
        )
        result = self.wait_feedback(feedback, "failed")
        self.assertEqual(result["reason"], "code")
        self.assertEqual(result["date"], "")

    def test_full_navigation_can_display_current_success(self) -> None:
        feedback = self.tracker()
        feedback.activate()
        self.show(f'<div id="response"><div class="alert-success">{NOTICE}</div></div>')
        self.wait_feedback(feedback, "success")

    def test_no_button_attempt_and_foreign_origin_cannot_succeed(self) -> None:
        feedback = self.tracker()
        self.page.run_js(
            "document.getElementById('response').innerHTML=arguments[0]",
            f'<div class="alert-success">{NOTICE}</div>',
        )
        self.assertEqual(feedback.read()["status"], "unknown")
        with self.assertRaisesRegex(ValueError, "renewal_page_unverified"):
            RenewalFeedback(FixturePage(self.page, "https://unrelated.invalid"))

    def test_actual_adblock_body_is_classified_before_login_or_form_checks(
        self,
    ) -> None:
        self.show("<h1>" + ADBLOCK_MESSAGE + "</h1>")
        browser = SimpleNamespace(page=FixturePage(self.page))
        self.assertEqual(hax_status(browser), "adblock")
        with self.assertRaises(AuthenticationError) as error:
            wait_hax_navigation(browser, "https://hax.co.id/vps-renew-code", timeout=0)
        self.assertEqual(error.exception.reason, "adblock_detected")
        self.assertEqual(
            hax_status(
                SimpleNamespace(
                    page=FixturePage(self.page, "https://unrelated.invalid")
                )
            ),
            "unknown",
        )

    def test_hidden_or_script_adblock_text_does_not_classify_normal_page(self) -> None:
        self.show(
            FORM
            + '<h1 style="display:none">'
            + ADBLOCK_MESSAGE
            + "</h1><script>const example="
            + json.dumps(ADBLOCK_MESSAGE)
            + ";</script>"
        )
        self.assertNotEqual(
            hax_status(SimpleNamespace(page=FixturePage(self.page))), "adblock"
        )

    def test_native_adblock_alert_remains_unanswered_by_classifier(self) -> None:
        self.page.run_js("setTimeout(() => alert(arguments[0]), 0)", ADBLOCK_MESSAGE)
        try:
            self.assertEqual(
                self.page.handle_alert(accept=None, timeout=2), ADBLOCK_MESSAGE
            )
            self.assertTrue(has_adblock_dialog(FixturePage(self.page)))
            self.assertEqual(
                self.page.handle_alert(accept=None, timeout=0), ADBLOCK_MESSAGE
            )
        finally:
            self.page.handle_alert(accept=False, timeout=0)

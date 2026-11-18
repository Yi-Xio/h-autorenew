"""通过正式求解入口验证验证码失败分类，不访问外部验证码服务。"""

import ast
import base64
import contextlib
import io
import json
import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import Mock, patch

import requests

from scripts.hax_renewal.flow import ArithmeticCaptchaUnavailable, CaptchaBlocked
from scripts.public_report import publish_progress, record_check
from tests.regression.fixtures import PROJECT


def captcha_functions() -> dict[str, Any]:
    """加载实际求解器，浏览器和音频服务由外部接口替身提供。"""
    names = {
        "CaptchaAudioUnavailable",
        "CaptchaSolver",
        "RecaptchaAudioSolver",
        "ArithmeticCaptchaSolver",
        "_prepare_final_form",
    }
    source = ast.parse((PROJECT / "hax-renew.py").read_text())
    classes: list[ast.stmt] = [
        node
        for node in source.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names
    ]
    namespace: dict[str, Any] = dict(
        CaptchaBlocked=CaptchaBlocked,
        ArithmeticCaptchaUnavailable=ArithmeticCaptchaUnavailable,
        Dict=Dict,
        Any=Any,
        Optional=Optional,
        re=re,
        Path=Path,
        os=os,
        HAS_DDDDOCR=False,
        HAS_SPEECH=True,
        record_check=record_check,
        requests=requests,
        time=time,
        _dbg=lambda text: None,
    )
    exec(
        compile(ast.Module(body=classes, type_ignores=[]), "hax-renew.py", "exec"),
        namespace,
    )
    return namespace


class CaptchaTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="hax-captcha-test-")
        self.addCleanup(temporary.cleanup)
        self.report = Path(temporary.name) / "report.json"
        environment = patch.dict(os.environ, PUBLIC_REPORT_FILE=str(self.report))
        environment.start()
        self.addCleanup(environment.stop)
        publish_progress("preparing")
        self.namespace = captcha_functions()
        self.elapsed = 0.0
        self.namespace["time"] = SimpleNamespace(
            monotonic=lambda: self.elapsed, sleep=self.advance_clock
        )

    def advance_clock(self, seconds: float) -> None:
        self.elapsed += seconds

    def check(self) -> dict[str, str]:
        """读取正式报告，核对对外提供的分类。"""
        return json.loads(self.report.read_text())["details"]["checks"]["captcha"]

    def arithmetic_browser(self, filled: bool | Exception) -> SimpleNamespace:
        """提供可解析的示例算式，以及浏览器回填的实际结果。"""

        def execute(script: str) -> object:
            if "img1:" in script:
                return dict(
                    img1="example-3104.28.1.1.jpg",
                    img2="example-4104.28.1.1.jpg",
                    op="+",
                )
            if isinstance(filled, Exception):
                raise filled
            return filled

        return SimpleNamespace(execute_script=execute)

    def test_arithmetic_requires_successful_input_and_never_reports_answers(
        self,
    ) -> None:
        solver = self.namespace["ArithmeticCaptchaSolver"]()
        for filled, result, answer in (
            (False, "input_missing", None),
            (RuntimeError("EXAMPLE_PRIVATE_VALUE"), "input_failed", None),
            (True, "passed", "7"),
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(solver.solve(self.arithmetic_browser(filled)), answer)
            self.assertEqual(self.check(), dict(step="arithmetic", result=result))
            self.assertNotIn("EXAMPLE_PRIVATE_VALUE", self.report.read_text())

    def audio_browser(
        self, status: str = "clear", audio_button: bool = True
    ) -> SimpleNamespace:
        """提供未完成的 reCAPTCHA 与业务需要的有限控件。"""

        def frame_script(script: str, **kwargs: object) -> object:
            if "getElementById('recaptcha-anchor')" in script:
                return True
            return status if "rc-doscaptcha" in script else False

        def element(selector: str, timeout: int) -> object:
            if selector == "#audio-response":
                return None
            return Mock() if audio_button else None

        frame = SimpleNamespace(
            run_js=frame_script,
            ele=element,
            frame_ele=SimpleNamespace(click=SimpleNamespace(at=lambda **kwargs: None)),
        )
        page = SimpleNamespace(
            get_frame=lambda *args, **kwargs: frame, get_frames=lambda: []
        )
        return SimpleNamespace(page=page, execute_script=lambda script: "")

    def test_recaptcha_distinguishes_server_block_and_visible_audio_error(self) -> None:
        for status in ("blocked", "audio_error"):
            with (
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaises(self.namespace["CaptchaBlocked"]),
            ):
                self.namespace["RecaptchaAudioSolver"](
                    self.audio_browser(status)
                ).solve()
            self.assertEqual(self.check(), dict(step="recaptcha", result=status))

    def test_missing_audio_control_is_not_reported_as_ip_block(self) -> None:
        with (
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(self.namespace["CaptchaAudioUnavailable"]),
        ):
            self.namespace["RecaptchaAudioSolver"](
                self.audio_browser(audio_button=False)
            ).solve()
        self.assertEqual(
            self.check(), dict(step="audio_challenge", result="button_missing")
        )

    def test_missing_anchor_and_dependency_return_explicit_failure(self) -> None:
        browser = self.audio_browser()
        browser.page.get_frame = lambda *args, **kwargs: None
        solver = self.namespace["RecaptchaAudioSolver"](browser)
        self.assertFalse(solver.solve())
        self.assertEqual(self.check(), dict(step="recaptcha", result="anchor_missing"))
        self.namespace["HAS_SPEECH"] = False
        self.assertFalse(solver.solve())
        self.assertEqual(
            self.check(), dict(step="recaptcha", result="dependency_missing")
        )

    def test_audio_transport_failure_stops_before_verify_and_keeps_details_private(
        self,
    ) -> None:
        for failure, expected in (
            (requests.exceptions.ConnectTimeout, "connect_timeout"),
            (requests.exceptions.ReadTimeout, "read_timeout"),
            (requests.exceptions.SSLError, "tls_failed"),
            (requests.exceptions.ProxyError, "proxy_failed"),
            (requests.exceptions.ConnectionError, "connection_failed"),
        ):
            browser = self.audio_browser()
            frame = browser.page.get_frame()
            verify = Mock()
            audio = SimpleNamespace(
                attr=lambda name: "https://www.google.com/recaptcha/api2/payload?p=EXAMPLE_PRIVATE_AUDIO"
            )
            frame.ele = lambda selector, timeout: (
                verify
                if selector == "#recaptcha-verify-button"
                else Mock()
                if selector == "#audio-response"
                else audio
            )
            browser.active = SimpleNamespace(
                url="https://hax.co.id/vps-renew-code",
                user_agent="Example Browser",
                cookies=lambda **kwargs: [],
            )
            with (
                patch.object(requests, "Session") as session,
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaises(self.namespace["CaptchaAudioUnavailable"]),
            ):
                session.return_value.get.side_effect = failure("EXAMPLE_PRIVATE_AUDIO")
                self.namespace["RecaptchaAudioSolver"](browser).solve()
            self.assertEqual(self.check(), dict(step="audio_download", result=expected))
            verify.click.assert_not_called()
            self.assertNotIn("EXAMPLE_PRIVATE_AUDIO", self.report.read_text())

    def test_delayed_anchor_initialization_is_ready_before_solving(self) -> None:
        browser = self.audio_browser()

        def frame_script(script: str, **kwargs: object) -> bool:
            if "getElementById('recaptcha-anchor')" in script:
                return self.elapsed >= 0.4
            return "aria-checked" in script

        frame = SimpleNamespace(run_js=frame_script)
        browser.page.get_frame = lambda *args, **kwargs: frame
        self.assertTrue(self.namespace["RecaptchaAudioSolver"](browser).solve())
        self.assertGreaterEqual(self.elapsed, 0.4)
        self.assertLess(self.elapsed, 5)
        self.assertEqual(self.check(), dict(step="recaptcha", result="passed"))

    def test_loading_and_unavailable_frames_remain_distinct(self) -> None:
        for unavailable, expected in (
            (False, "anchor_loading"),
            (True, "frame_unavailable"),
        ):
            self.elapsed = 0
            browser = self.audio_browser()

            def frame_script(script: str, **kwargs: object) -> bool:
                if unavailable:
                    raise RuntimeError("EXAMPLE_PRIVATE_FRAME")
                return False

            browser.page.get_frame = lambda *args, **kwargs: SimpleNamespace(
                run_js=frame_script
            )
            self.assertFalse(self.namespace["RecaptchaAudioSolver"](browser).solve())
            self.assertEqual(self.check(), dict(step="recaptcha", result=expected))
            self.assertEqual(self.elapsed, 5)
            self.assertNotIn("EXAMPLE_PRIVATE_FRAME", self.report.read_text())

    def test_incomplete_or_unavailable_recaptcha_stops_final_form_preparation(
        self,
    ) -> None:
        for dependency, failure, inspection in (
            (True, False, False),
            (True, True, False),
            (False, False, False),
            (True, False, True),
        ):
            events: list[str] = []

            def execute(script: str) -> bool:
                if "var iframes" in script:
                    if inspection:
                        raise RuntimeError("EXAMPLE_PRIVATE_INSPECTION")
                    return True
                events.append("form_changed")
                return True

            def solve() -> bool:
                if failure:
                    raise RuntimeError("EXAMPLE_PRIVATE_SOLVER")
                return False

            browser = SimpleNamespace(
                execute_script=execute,
                page=SimpleNamespace(
                    ele=lambda *args, **kwargs: events.append("click")
                ),
                driver=SimpleNamespace(
                    title="Example",
                    switch_to=SimpleNamespace(default_content=lambda: None),
                ),
            )
            self.namespace.update(
                HAS_SPEECH=dependency,
                RecaptchaAudioSolver=lambda browser: SimpleNamespace(solve=solve),
            )
            with (
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaises(self.namespace["CaptchaAudioUnavailable"]),
            ):
                self.namespace["_prepare_final_form"](
                    browser, 1, "example"
                )
            self.assertEqual(events, [])
            self.assertNotIn("EXAMPLE_PRIVATE", self.report.read_text())


@unittest.skipUnless(
    os.environ.get("HAX_BROWSER_TESTS") == "1", "启用真实 Chrome 示例页面"
)
class CaptchaBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from DrissionPage import ChromiumOptions, ChromiumPage

        temporary = tempfile.TemporaryDirectory(prefix="hax-captcha-browser-")
        cls.addClassCleanup(temporary.cleanup)
        options = ChromiumOptions().auto_port().headless()
        options.set_tmp_path(str(Path(temporary.name) / "browser"))
        options.set_argument("--no-sandbox")
        options.set_argument("--disable-dev-shm-usage")
        cls.browser = ChromiumPage(options).browser
        cls.addClassCleanup(cls.browser.quit, timeout=5, force=True, del_data=True)

    def test_real_iframe_waits_for_delayed_checkbox_without_external_services(
        self,
    ) -> None:
        page = self.browser.new_tab("about:blank")
        self.addCleanup(page.close)
        temporary = tempfile.TemporaryDirectory(prefix="hax-captcha-report-")
        self.addCleanup(temporary.cleanup)
        report = Path(temporary.name) / "report.json"
        with patch.dict(os.environ, PUBLIC_REPORT_FILE=str(report)):
            publish_progress("preparing")

            def paused(**value: Any) -> None:
                if value["request"]["url"].startswith("https://hax.co.id/"):
                    body = """<div class='g-recaptcha'></div><script>
                        setTimeout(() => {
                            const frame = document.createElement('iframe');
                            frame.src='https://www.google.com/recaptcha/api2/anchor';
                            document.body.append(frame);
                        }, 200);
                    </script>"""
                else:
                    body = """<script>setTimeout(() => {
                        document.body.innerHTML='<div id="recaptcha-anchor" role="checkbox" aria-checked="true">Example</div>';
                    }, 400);</script>"""
                page.run_cdp(
                    "Fetch.fulfillRequest",
                    requestId=value["requestId"],
                    responseCode=200,
                    responseHeaders=[{"name": "Content-Type", "value": "text/html"}],
                    body=base64.b64encode(
                        ("<!doctype html><body>" + body).encode()
                    ).decode(),
                )

            page.driver.set_callback("Fetch.requestPaused", paused)
            page.run_cdp("Fetch.enable", patterns=[{"urlPattern": "*"}])
            page.get("https://hax.co.id/vps-renew-code")
            browser = SimpleNamespace(page=page, execute_script=page.run_js)
            self.assertTrue(
                captcha_functions()["RecaptchaAudioSolver"](browser).solve()
            )
        self.assertEqual(
            json.loads(report.read_text())["details"]["checks"]["captcha"],
            dict(step="recaptcha", result="passed"),
        )

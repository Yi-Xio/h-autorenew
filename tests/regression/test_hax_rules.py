"""在无网示例页面验证 Hax 规则的时机、请求范围和浏览器生命周期。"""

import base64
import contextlib
import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts.browser_setup import (
    ADBLOCK_MESSAGE,
    RULES_FILE,
    has_adblock_dialog,
    install_hax_rules,
)
from scripts.web_sessions import AuthenticationError
from scripts.hax_login import wait_hax_navigation

PROBE_URL = "https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js"


class RegistrationPage:
    """提供文档前脚本的示例注册结果，不连接浏览器。"""

    def __init__(self) -> None:
        self.run_cdp = Mock(return_value=dict(identifier="example-registration"))


class RegistrationTests(unittest.TestCase):
    def test_registration_is_per_tab_and_idempotent(self) -> None:
        first, second = RegistrationPage(), RegistrationPage()
        install_hax_rules(first)
        install_hax_rules(first)
        install_hax_rules(second)
        for page in (first, second):
            page.run_cdp.assert_called_once()
            self.assertEqual(
                page.run_cdp.call_args.args,
                ("Page.addScriptToEvaluateOnNewDocument",),
            )

    def test_file_or_driver_failure_stops_without_publishing_raw_details(self) -> None:
        for source, response, failure in (
            ("", dict(identifier="1"), None),
            ("example", {}, None),
            ("example", dict(identifier=True), None),
            ("example", None, RuntimeError("EXAMPLE_PRIVATE_VALUE")),
        ):
            page = RegistrationPage()
            page.run_cdp.return_value = response
            page.run_cdp.side_effect = failure
            output = io.StringIO()
            with (
                self.subTest(
                    source_empty=not source, driver_failed=failure is not None
                ),
                patch("scripts.browser_setup.RULES_FILE") as file,
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(output),
                self.assertRaises(AuthenticationError) as error,
            ):
                file.read_text.return_value = source
                install_hax_rules(page)
            self.assertEqual(error.exception.reason, "hax_rules_unavailable")
            self.assertEqual(output.getvalue(), "")
        with (
            patch("scripts.browser_setup.RULES_FILE") as file,
            self.assertRaises(AuthenticationError) as error,
        ):
            file.read_text.side_effect = OSError("EXAMPLE_PRIVATE_VALUE")
            install_hax_rules(RegistrationPage())
        self.assertEqual(error.exception.reason, "hax_rules_unavailable")

    def test_known_hax_dialog_is_read_without_accepting_any_dialog(self) -> None:
        for message in (ADBLOCK_MESSAGE, "Confirm account", "Renew VPS?"):
            handler = Mock(return_value=message)
            page = SimpleNamespace(url="https://hax.co.id/login", handle_alert=handler)
            self.assertEqual(has_adblock_dialog(page), message == ADBLOCK_MESSAGE)
            handler.assert_called_once_with(accept=None, timeout=0)
        handler = Mock(return_value=ADBLOCK_MESSAGE)
        self.assertFalse(
            has_adblock_dialog(
                SimpleNamespace(
                    url="https://unrelated.invalid/login", handle_alert=handler
                )
            )
        )
        handler.assert_not_called()


@unittest.skipUnless(
    os.environ.get("HAX_BROWSER_TESTS") == "1", "启用真实 Chrome 示例页面"
)
class HaxRulesBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from DrissionPage import ChromiumOptions, ChromiumPage

        temporary = tempfile.TemporaryDirectory(prefix="hax-rules-browser-")
        cls.addClassCleanup(temporary.cleanup)
        options = ChromiumOptions().auto_port().headless()
        options.set_tmp_path(str(Path(temporary.name) / "browser"))
        options.set_argument("--no-sandbox")
        options.set_argument("--disable-dev-shm-usage")
        cls.browser = ChromiumPage(options).browser
        cls.addClassCleanup(cls.browser.quit, timeout=5, force=True, del_data=True)

    def setUp(self) -> None:
        self.requests = []
        self.body = "<button id='renew'>Renew</button>"
        self.fail_probe = False
        self.pending_resource = False
        self.page = self.new_page()

    def new_page(self):
        page = self.browser.new_tab("about:blank")
        self.addCleanup(page.close)

        def paused(**value):
            request = value["request"]
            self.requests.append(request)
            if self.pending_resource and request["url"].endswith("/pending-resource"):
                return
            if request["url"] == PROBE_URL and self.fail_probe:
                page.run_cdp(
                    "Fetch.failRequest",
                    requestId=value["requestId"],
                    errorReason="Failed",
                )
                return
            body = (
                "<!doctype html><html><body>" + self.body + "</body></html>"
                if value.get("resourceType") == "Document"
                else "ordinary-response"
            )
            page.run_cdp(
                "Fetch.fulfillRequest",
                requestId=value["requestId"],
                responseCode=200,
                responseHeaders=[
                    {"name": "Content-Type", "value": "text/html"},
                    {"name": "Access-Control-Allow-Origin", "value": "*"},
                ],
                body=base64.b64encode(body.encode()).decode(),
            )

        # 所有示例响应由 CDP 提供，不访问真实 Hax、OAuth、验证码或广告服务。
        page.driver.set_callback("Fetch.requestPaused", paused)
        page.run_cdp("Fetch.enable", patterns=[{"urlPattern": "*"}])
        return page

    def show(self, origin="https://hax.co.id", page=None, rules=True) -> None:
        page = page or self.page
        if rules:
            install_hax_rules(page)
        page.get(origin + "/document")

    def evaluate(self, expression, page=None):
        response = (page or self.page).run_cdp(
            "Runtime.evaluate",
            expression=expression,
            awaitPromise=True,
            returnByValue=True,
        )
        self.assertNotIn("exceptionDetails", response)
        return response.get("result", {}).get("value")

    def test_probe_finishes_when_advertising_network_is_unavailable(self) -> None:
        self.fail_probe = True
        self.body += """<script>
            document.addEventListener('DOMContentLoaded', () => {
                window.checked=fetch('https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js')
                    .then(() => true).catch(() => {document.body.innerHTML='AdBlock'; return false;});
            });
        </script>"""
        self.show()
        self.assertTrue(self.evaluate("window.checked"))
        self.assertTrue(self.evaluate("!!document.getElementById('renew')"))
        self.assertFalse(any(request["url"] == PROBE_URL for request in self.requests))
        other = self.new_page()
        self.show(page=other, rules=False)
        self.assertFalse(self.evaluate("window.checked", other))
        self.assertEqual(self.evaluate("document.body.innerText", other), "AdBlock")

    def test_string_url_and_request_inputs_return_usable_response(self) -> None:
        self.show()
        result = self.evaluate("""(async () => {
            const url='https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js';
            const values=[];
            for (const input of [url, new URL(url), new Request(url)]) {
                const response=await fetch(input);
                values.push([response.ok, response.status, await response.json()]);
            }
            return values;
        })()""")
        self.assertEqual(result, [[True, 200, {}]] * 3)
        self.assertFalse(any(request["url"] == PROBE_URL for request in self.requests))

    def test_known_load_detector_cannot_clear_page(self) -> None:
        self.body += """<script>
            window.addEventListener('load', () => {
                document.body.innerHTML='Please disable AdBlock to access this site!';
            });
        </script>"""
        self.show()
        self.assertTrue(self.evaluate("!!document.getElementById('renew')"))
        other = self.new_page()
        self.show(page=other, rules=False)
        self.assertEqual(
            self.evaluate("document.body.innerText", other), ADBLOCK_MESSAGE
        )

    def test_other_urls_methods_and_request_bodies_reach_original_fetch(self) -> None:
        self.show()
        result = self.evaluate("""(async () => {
            const url='https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js';
            const requests=[url+'?version=2', '/renewal-code', url.replace('pagead2.', 'other.'),
                [url,{method:'POST',body:'example-body'}],
                new Request(url,{method:'POST',body:'example-request-body'})];
            const values=[];
            for (const input of requests) {
                const response=await (Array.isArray(input) ? fetch(...input) : fetch(input));
                values.push(await response.text());
            }
            return values;
        })()""")
        self.assertEqual(result, ["ordinary-response"] * 5)
        posts = [request for request in self.requests if request["method"] == "POST"]
        self.assertEqual(
            [request.get("postData") for request in posts],
            ["example-body", "example-request-body"],
        )

    def test_invalid_and_cancelled_requests_keep_failure_semantics(self) -> None:
        self.show()
        result = self.evaluate("""(async () => {
            const url='https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js';
            const controller=new AbortController();
            controller.abort('example-cancelled');
            const cancelled=await fetch(url,{signal:controller.signal}).then(() => false, error => error);
            const invalid=await fetch(url,{method:'GET',body:'example-body'}).then(() => false, error => error.name);
            return {cancelled,invalid};
        })()""")
        self.assertEqual(
            result, dict(cancelled="example-cancelled", invalid="TypeError")
        )
        self.assertFalse(any(request["url"] == PROBE_URL for request in self.requests))

    def test_rule_does_not_apply_to_other_origins_or_ports(self) -> None:
        for origin in (
            "https://oauth.telegram.org",
            "https://challenges.cloudflare.com",
            "https://hax.co.id.example.invalid",
            "https://hax.co.id:444",
        ):
            with self.subTest(origin=origin):
                self.show(origin)
                self.assertEqual(self.evaluate("location.origin"), origin)
                result = self.evaluate(
                    "fetch('https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js').then(r => r.text())"
                )
                self.assertEqual(result, "ordinary-response")

    def test_http_origin_does_not_install_rules(self) -> None:
        self.show(rules=False)
        # Chrome 会将该域的 HTTP 导航升级为 HTTPS；直接执行同一脚本验证协议条件。
        self.evaluate(
            "(function(location){"
            + RULES_FILE.read_text()
            + "})({origin:'http://hax.co.id'})"
        )
        self.assertEqual(
            self.evaluate("fetch('" + PROBE_URL + "').then(r => r.text())"),
            "ordinary-response",
        )

    def test_rule_survives_navigation_and_applies_to_new_tabs(self) -> None:
        self.show()
        self.page.refresh()
        for page in (self.page, self.new_page()):
            self.show(page=page)
            self.assertEqual(
                self.evaluate("fetch('" + PROBE_URL + "').then(r => r.json())", page),
                {},
            )

    def test_duplicate_script_and_normal_events_preserve_page_behavior(self) -> None:
        self.show()
        self.evaluate(
            "window.firstFetch=window.fetch; window.firstListener=EventTarget.prototype.addEventListener"
        )
        self.evaluate(RULES_FILE.read_text())
        result = self.evaluate("""(() => {
            let clicks=0, loads=0, downloads=0;
            const button=document.getElementById('renew');
            button.addEventListener('click', () => {clicks++; return 'Please disable AdBlock to access this site!';});
            button.click();
            window.addEventListener('load', () => {loads++; return 'AdBlock help';});
            window.dispatchEvent(new Event('load'));
            window.addEventListener('download', () => {downloads++; return 'Please disable AdBlock to access this site!';});
            window.dispatchEvent(new Event('download'));
            return {sameFetch:window.firstFetch===window.fetch,
                sameListener:window.firstListener===EventTarget.prototype.addEventListener,clicks,loads,downloads};
        })()""")
        self.assertEqual(
            result,
            dict(sameFetch=True, sameListener=True, clicks=1, loads=1, downloads=1),
        )

    def show_pending_form(
        self,
        body: str,
        *,
        parsed: bool = True,
        rules: bool = True,
        deferred: bool = False,
        path: str = "/vps-renew-code",
        origin: str = "https://hax.co.id",
    ):
        self.pending_resource = True
        resource = '<img src="/pending-resource">'
        if not parsed:
            resource = '<script src="/pending-resource"></script>'
        elif deferred:
            resource = '<script defer src="/pending-resource"></script>'
        self.body = body + resource
        page = self.new_page()
        page.set.load_mode.none()
        page.set.timeouts(base=0.2, page_load=0.2)
        if rules:
            install_hax_rules(page)
        page.get(origin + path)
        deadline = time.monotonic() + 3
        desired = "interactive" if parsed else "loading"
        while True:
            observed = self.evaluate(
                "({path:location.pathname,state:document.readyState,resource:!!document.querySelector('[src=\"/pending-resource\"]'),dom:(performance.getEntriesByType('navigation')[0]?.domContentLoadedEventEnd || 0)>0})",
                page,
            )
            if (
                observed["path"] == path
                and observed["state"] == desired
                and observed["resource"]
                and (not parsed or deferred or observed["dom"])
            ):
                break
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        return page

    def test_parsed_business_form_does_not_wait_for_unrelated_resource(self) -> None:
        page = self.show_pending_form(
            '<a href="https://hax.co.id/logout">Logout</a>'
            '<input name="code"><button name="submit_button">Renew VPS</button>'
        )
        wait_hax_navigation(
            SimpleNamespace(page=page), "https://hax.co.id/vps-renew-code", timeout=0.2
        )
        self.assertEqual(self.evaluate("document.readyState", page), "interactive")

    def test_deferred_script_must_finish_before_business_page_is_accepted(self) -> None:
        page = self.show_pending_form(
            '<a href="https://hax.co.id/logout">Logout</a>'
            '<input name="code"><button name="submit_button">Renew VPS</button>',
            deferred=True,
        )
        self.assertEqual(self.evaluate("document.readyState", page), "interactive")
        self.assertFalse(
            self.evaluate(
                "performance.getEntriesByType('navigation')[0].domContentLoadedEventEnd>0",
                page,
            )
        )
        with self.assertRaises(AuthenticationError) as error:
            wait_hax_navigation(
                SimpleNamespace(page=page),
                "https://hax.co.id/vps-renew-code",
                timeout=0.2,
            )
        self.assertEqual(error.exception.reason, "hax_page_unverified")

    def test_pending_resources_do_not_bypass_document_and_form_checks(
        self,
    ) -> None:
        logout = '<a href="https://hax.co.id/logout">Logout</a>'
        code = '<input name="code">'
        button = '<button name="submit_button">Renew VPS</button>'
        for body, parsed, rules, path, origin in (
            (code + button, True, True, "/vps-renew-code", "https://hax.co.id"),
            (logout + button, True, True, "/vps-renew-code", "https://hax.co.id"),
            (logout + code, True, True, "/vps-renew-code", "https://hax.co.id"),
            (logout + code + button, True, True, "/vps-info", "https://hax.co.id"),
            (
                logout + code + button,
                True,
                False,
                "/vps-renew-code",
                "https://hax.co.id",
            ),
            (
                logout + code + button,
                False,
                True,
                "/vps-renew-code",
                "https://hax.co.id",
            ),
            (
                logout + code + button,
                True,
                True,
                "/vps-renew-code",
                "https://other.example.invalid",
            ),
        ):
            with self.subTest(
                parsed=parsed, rules=rules, path=path, origin=origin, body=body
            ):
                page = self.show_pending_form(
                    body, parsed=parsed, rules=rules, path=path, origin=origin
                )
                with self.assertRaises(AuthenticationError) as error:
                    wait_hax_navigation(
                        SimpleNamespace(page=page),
                        "https://hax.co.id/vps-renew-code",
                        timeout=0.2,
                    )
                self.assertEqual(error.exception.reason, "hax_page_unverified")

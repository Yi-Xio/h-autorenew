"""用实际 WARP 方法和本地 CLI 替身验证出口判断，不连接真实网络。"""

import ast
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from ipaddress import IPv4Address
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import patch

from scripts.job_summary import REASON_LABELS
from scripts.public_report import WARP_FAILURE_REASONS

PROJECT = Path(__file__).resolve().parents[2]
CLI_SOURCE = r"""
import json,os,sys
from pathlib import Path
path=Path(os.environ['EXAMPLE_WARP_STATE'])
state=json.loads(path.read_text())
args=sys.argv[1:]
state['commands'].append([Path(sys.argv[0]).name,*args])
code=0
output=''
if Path(sys.argv[0]).name=='curl':
    if args[-1]=='https://api.ipify.org':
        output=state['ip'] if state.get('ip_response') is None else state['ip_response']
        if os.environ.get('HTTPS_PROXY') and '--noproxy' not in args and state.get('proxy_ip'):
            output=state['proxy_ip']
        code=22 if state.get('query_error') else 0
    elif args[-1]=='https://www.cloudflare.com/cdn-cgi/trace':
        output='warp='+('on' if state['ready'] else 'off')+'\nip='+state['ip']+'\n'
        code=22 if state.get('trace_error') else 0
    else: code=2
elif Path(sys.argv[0]).name=='sudo' and args[:2]==['warp-cli','--accept-tos']:
    command=args[2:]
    if command==['disconnect']: state['ready']=False
    elif command==['registration','delete']: pass
    elif command==['registration','new']:
        code=1 if state.get('registration_error') else 0
        if code==0 and state.get('next_ips'): state['ip']=state['next_ips'].pop(0)
    elif command==['connect']:
        code=1 if state.get('connect_error') else 0
        state['ready']=code==0 and state.get('warp_on_connect',True)
    else: code=2
else: code=2
path.write_text(json.dumps(state))
sys.stdout.write(output)
sys.exit(code)
"""


class Clock:
    """CLI 返回后推进等待时间，不为回归实际等待网络超时。"""

    def __init__(self) -> None:
        self.elapsed = 0.0

    def monotonic(self) -> float:
        return self.elapsed

    def sleep(self, seconds: float) -> None:
        self.elapsed += seconds


def warp_functions(clock: Clock) -> dict[str, Any]:
    """加载源文件实际类及业务函数，避免启动旧认证和浏览器依赖。"""
    names = {
        "WarpError",
        "CaptchaBlocked",
        "ArithmeticCaptchaUnavailable",
        "WarpManager",
        "ensure_warp_ready",
        "restart_warp",
        "renew",
        "_submit_code_page",
    }
    source = ast.parse((PROJECT / "hax-renew.py").read_text())
    nodes: list[ast.stmt] = [
        node
        for node in source.body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names
    ]
    # 实际类在运行时加载，动态命名空间只供这些源函数使用。
    namespace: dict[str, Any] = dict(
        subprocess=subprocess,
        time=clock,
        IPv4Address=IPv4Address,
        WARP_CMD_TIMEOUT=2,
        WARP_FAILURE_REASONS=WARP_FAILURE_REASONS,
        REASON_LABELS=REASON_LABELS,
        MAX_WARP_ROTATE_ATTEMPTS=2,
        Dict=Dict,
        Any=Any,
        Optional=Optional,
        re=re,
        datetime=datetime,
        timedelta=timedelta,
        timezone=timezone,
    )
    exec(
        compile(ast.Module(body=nodes, type_ignores=[]), "hax-renew.py", "exec"),
        namespace,
    )
    return namespace


class WarpTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="hax-warp-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state.json"
        self.configure()
        for name in ("sudo", "curl"):
            command = self.root / name
            command.write_text("#!" + sys.executable + "\n" + CLI_SOURCE)
            command.chmod(0o700)
        environment = patch.dict(
            os.environ,
            PATH=str(self.root) + os.pathsep + os.environ.get("PATH", ""),
            EXAMPLE_WARP_STATE=str(self.state),
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.clock = Clock()
        self.namespace = warp_functions(self.clock)
        self.manager = self.namespace["WarpManager"]()
        self.namespace["get_warp_manager"] = lambda: self.manager
        self.output = io.StringIO()
        quiet = contextlib.redirect_stdout(self.output)
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def configure(self, **changes: object) -> None:
        """配置示例 CLI 的外部结果，保留前一操作的状态。"""
        value: dict[str, object] = (
            json.loads(self.state.read_text())
            if self.state.exists()
            else dict(ready=True, ip="198.51.100.1", next_ips=[], commands=[])
        )
        value.update(changes)
        self.state.write_text(json.dumps(value))

    def commands(self) -> list[list[str]]:
        """读取替身 CLI 收到的调用，不读取被测类内部状态。"""
        return json.loads(self.state.read_text())["commands"]

    def test_ready_connection_reuses_recorded_ip_without_registration(self) -> None:
        self.manager.record_initial_ip()
        self.assertTrue(self.manager.ensure_ready(max_attempts=2))
        self.assertEqual(self.manager.failure_reason, "none")
        self.assertEqual(
            (self.manager.rotation_count, self.manager.warp_refresh_count), (0, 0)
        )
        self.assertFalse(any(args[0] == "sudo" for args in self.commands()))

    def test_disconnected_preparation_accepts_same_ip_after_connection(self) -> None:
        self.manager.record_initial_ip()
        self.configure(ready=False)
        self.namespace["ensure_warp_ready"]()
        self.assertEqual(
            (self.manager.rotation_count, self.manager.warp_refresh_count), (0, 1)
        )
        self.assertEqual(self.manager.failure_reason, "none")

    def test_strict_rotation_rejects_same_ip_even_without_initial_record(self) -> None:
        with self.assertRaises(self.namespace["WarpError"]) as error:
            self.namespace["restart_warp"]()
        self.assertEqual(error.exception.reason, "warp_new_exit_unavailable")
        self.assertEqual(
            (self.manager.rotation_count, self.manager.warp_refresh_count), (0, 2)
        )

    def test_strict_rotation_counts_only_new_accepted_exits(self) -> None:
        self.manager.record_initial_ip()
        self.configure(next_ips=["198.51.100.1", "198.51.100.2"])
        self.assertTrue(self.manager.rotate_ip(max_attempts=2))
        self.configure(next_ips=["198.51.100.1", "198.51.100.3"])
        self.assertTrue(self.manager.rotate_ip(max_attempts=2))
        self.assertEqual(
            (self.manager.rotation_count, self.manager.warp_refresh_count), (2, 4)
        )

    def test_proxy_environment_is_not_used_to_compare_direct_exits(self) -> None:
        self.configure(proxy_ip="203.0.113.99", next_ips=["198.51.100.2"])
        with patch.dict(os.environ, HTTPS_PROXY="http://proxy.example.invalid:3128"):
            self.manager.record_initial_ip()
            self.assertTrue(self.manager.rotate_ip(max_attempts=1))
        self.assertEqual(self.manager.rotation_count, 1)

    def test_invalid_or_failed_ip_query_is_not_accepted(self) -> None:
        for response in (
            "",
            "<html>EXAMPLE_PRIVATE_VALUE</html>",
            "not-an-ip",
            "2001:db8::1",
        ):
            self.configure(ip_response=response, commands=[])
            self.assertFalse(self.manager.ensure_ready(max_attempts=1))
            self.assertEqual(self.manager.failure_reason, "warp_ip_query_failed")
            self.assertFalse(any(args[0] == "sudo" for args in self.commands()))
        self.configure(ip_response=None, query_error=True)
        self.assertFalse(self.manager.rotate_ip(max_attempts=1))
        self.assertEqual(self.manager.failure_reason, "warp_ip_query_failed")

    def test_registration_and_connection_failures_are_separate(self) -> None:
        for changes, reason in (
            (dict(registration_error=True), "warp_registration_failed"),
            (dict(connect_error=True), "warp_connection_failed"),
            (dict(warp_on_connect=False), "warp_connection_failed"),
            (dict(trace_error=True), "warp_connection_failed"),
        ):
            self.configure(
                **dict(
                    dict(
                        ready=False,
                        registration_error=False,
                        connect_error=False,
                        warp_on_connect=True,
                        trace_error=False,
                    ),
                    **changes,
                )
            )
            self.assertFalse(self.manager.ensure_ready(max_attempts=1))
            self.assertEqual(self.manager.failure_reason, reason)

    def business(self) -> tuple[dict[str, Any], list[str], object]:
        """业务函数保留实际网络判断，浏览器、消息服务和通知使用替身。"""
        namespace = self.namespace
        events: list[str] = []
        browser = SimpleNamespace(
            get_page_source=lambda: "<form></form>",
            get_title=lambda: "Renew",
        )
        namespace.update(
            _parse_iso_date=lambda text: None,
            cn_now=lambda: datetime.now(),
            mask_phone=lambda text: "example",
            safe_sid_for_filename=lambda text: text,
            MAX_RENEW_RETRIES_PER_URL=0,
            MAX_WARP_RETRIES=1,
            HAX_VPS_RENEW_CODE_URL="https://hax.co.id/vps-renew-code",
            _renew_fill_form=lambda *args: None,
            _renew_start_tg_listener=lambda *args, **kwargs: ({}, None),
            _renew_navigate_to_code_page=lambda *args: None,
            handoff_browser_to_warp=lambda *args: True,
            dismiss_cookie_only=lambda *args: None,
            removeAds=lambda *args: None,
            solve_arithmetic_captcha=lambda *args, **kwargs: None,
            safe_screenshot=lambda *args: None,
            shot=lambda *args: "example",
            _renew_get_code=lambda *args: "EXAMPLE_BUSINESS_CODE",
            _submit_code_page=lambda *args: events.append("submitted"),
            notify=lambda *args, **kwargs: None,
        )

        def feedback(page: object, result: dict[str, Any], old: str) -> bool:
            result["success"] = True
            return True

        namespace["_renew_parse_result"] = feedback
        return namespace, events, browser

    def test_final_code_page_stops_when_required_ip_change_fails(self) -> None:
        events = []

        def captcha_refused() -> None:
            raise self.namespace["CaptchaBlocked"]("example captcha failure")

        self.namespace.update(
            HAS_SPEECH=True,
            RecaptchaAudioSolver=lambda browser: SimpleNamespace(solve=captcha_refused),
        )
        browser = SimpleNamespace(
            driver=SimpleNamespace(
                switch_to=SimpleNamespace(default_content=lambda: None)
            ),
            execute_script=lambda script: True,
            open=lambda url: events.append("opened"),
        )
        with self.assertRaises(self.namespace["WarpError"]) as error:
            self.namespace["_submit_code_page"](
                browser, "EXAMPLE_BUSINESS_CODE", 1, "example", max_retries=2
            )
        self.assertEqual(error.exception.reason, "warp_new_exit_unavailable")
        self.assertEqual(events, [])

    def test_first_business_attempt_can_submit_on_existing_exit(self) -> None:
        namespace, events, browser = self.business()
        result = namespace["renew"](browser, {"id": "example"}, 1, "+1-5550001234")
        self.assertTrue(result["success"], result.get("message"))
        self.assertEqual(events, ["submitted"])
        self.assertEqual(result["warp_swaps"], 0)
        self.assertEqual(result["warp_refresh"], 0)

    def test_failed_strict_recovery_stops_before_another_submission(self) -> None:
        namespace, events, browser = self.business()
        namespace["MAX_RENEW_RETRIES_PER_URL"] = 2
        attempts = []

        def refused(*args: object) -> None:
            attempts.append("failed")
            raise namespace["CaptchaBlocked"]("example captcha failure")

        namespace["_renew_fill_form"] = refused
        result = namespace["renew"](browser, {"id": "example"}, 1, "+1-5550001234")
        self.assertFalse(result["success"])
        self.assertEqual(result["public_reason"], "warp_new_exit_unavailable")
        self.assertEqual(events, [])
        self.assertEqual(attempts, ["failed"])

    def test_outer_retry_still_requires_a_different_exit(self) -> None:
        namespace, events, browser = self.business()
        namespace["MAX_RENEW_RETRIES_PER_URL"] = 2
        namespace["_renew_parse_result"] = lambda *args: False
        result = namespace["renew"](browser, {"id": "example"}, 1, "+1-5550001234")
        self.assertFalse(result["success"])
        self.assertEqual(result["public_reason"], "warp_new_exit_unavailable")
        self.assertEqual(events, ["submitted"])

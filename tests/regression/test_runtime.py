"""从正式 CLI 验证公开输出、结果状态与进程清理。"""

import json
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from scripts.accounts import state_key
from tests.regression.fixtures import web_account, web_environment, navigation_sample
from scripts.renewal_config import account_masks

from tests.regression.fixtures import ACCOUNT, SECOND, PROJECT, environment


class RuntimeTests(unittest.TestCase):
    """将替身 worker 当作上游程序，通过入口观察公开行为。"""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="hax-test-runtime-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        shutil.copytree(
            PROJECT / "scripts",
            self.root / "scripts",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        self.env = environment()
        self.env.update(
            {"BATCH": ACCOUNT, "PUBLIC_HEARTBEAT_SECONDS": "1", **web_environment(2)}
        )

    def worker(self, body: str, platform: str = "hax") -> None:
        """安装执行入口约定位置的替身脚本。"""
        prefix = "import os, sys, json\nfrom pathlib import Path\nfrom scripts.public_report import publish_progress, publish_results\n"
        (self.root / f"{platform}-renew.py").write_text(prefix + body, encoding="utf-8")

    def invoke(self, platform: str = "hax") -> subprocess.CompletedProcess[str]:
        """像工作流一样调用正式 CLI。"""
        return subprocess.run(
            [sys.executable, "-m", "scripts.actions_runtime", platform],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=15,
        )

    def assert_private(self, result: subprocess.CompletedProcess[str]) -> None:
        """所有公开输出都不应包含替身凭据。"""
        output = result.stdout + result.stderr
        for value in (ACCOUNT + "," + SECOND).split(","):
            self.assertNotIn(value, output)
        for number in (1, 2):
            for value in account_masks(web_account(number)):
                self.assertNotIn(value, output)
        self.assertNotIn("Traceback", output)

    def test_fixed_navigation_conditions_reach_runtime_without_page_text(self) -> None:
        sample = json.dumps(navigation_sample("loading"))
        self.worker(
            "from scripts.public_report import record_navigation, record_check\n"
            "from scripts.renewal_config import worker_account\n"
            "account=worker_account(os.environ)\n"
            "publish_progress('preparing')\n"
            f"record_navigation(json.loads({sample!r}))\n"
            "record_check('captcha', 'audio_download', 'http_failed')\n"
            "sys.exit(publish_results([{'username':account['phone'],'servers':[], 'public_state':'no_vps'}], 'hax'))\n"
        )
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("stage=navigation", result.stdout)
        self.assertIn("document_state=loading", result.stdout)
        self.assertIn(
            "stage=diagnostic check=captcha step=audio_download result=http_failed",
            result.stdout,
        )
        value = json.loads((self.root / "output/renewal-result.json").read_text())
        self.assertEqual(value["details"]["navigation"], [navigation_sample("loading")])
        self.assert_private(result)

    def test_diagnostic_free_text_from_worker_cannot_be_published(self) -> None:
        self.worker(
            "from scripts.renewal_config import worker_account\n"
            "account=worker_account(os.environ)\n"
            "publish_results([{'username':account['phone'],'servers':[], 'public_state':'no_vps'}], 'hax')\n"
            "path=Path(os.environ['PUBLIC_REPORT_FILE'])\n"
            "value=json.loads(path.read_text())\n"
            "value['details']['checks']={'captcha':{'step':'audio_source','result':'EXAMPLE_PRIVATE_VALUE'}}\n"
            "path.write_text(json.dumps(value))\n"
        )
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("EXAMPLE_PRIVATE_VALUE", result.stdout + result.stderr)
        self.assert_private(result)

    def test_navigation_free_text_from_worker_cannot_be_published(self) -> None:
        sample = json.dumps(
            dict(navigation_sample(), current_route="EXAMPLE_PRIVATE_VALUE")
        )
        self.worker(
            "from scripts.renewal_config import worker_account\n"
            "account=worker_account(os.environ)\n"
            "publish_results([{'username':account['phone'],'servers':[], 'public_state':'no_vps'}], 'hax')\n"
            "path=Path(os.environ['PUBLIC_REPORT_FILE'])\n"
            "value=json.loads(path.read_text())\n"
            f"value['details']['navigation']=[json.loads({sample!r})]\n"
            "path.write_text(json.dumps(value))\n"
        )
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("EXAMPLE_PRIVATE_VALUE", result.stdout + result.stderr)
        self.assert_private(result)

    def test_raw_streams_threads_subprocesses_and_files_stay_private(self) -> None:
        self.env.update(
            {
                "BATCH": ACCOUNT + ";" + SECOND,
                "BATCH_ACCOUNT_INDEX": "2",
                "RENEWAL_ACCOUNT_ID": "A002",
                "GITHUB_OUTPUT": str(self.root / "github-output"),
                "GITHUB_ENV": str(self.root / "environment"),
                "GITHUB_STEP_SUMMARY": str(self.root / "summary"),
                "TEST_LOCATION": str(self.root / "location"),
            }
        )
        self.worker("""
import base64, subprocess, threading
from urllib.parse import quote
from scripts.renewal_config import worker_account, account_masks
account = worker_account(os.environ)
assert account["phone"] == "+1-5550005678"
assert not any(name in os.environ for name in ("BATCH", "RENEWAL_ACCOUNTS", "RENEWAL_ACCOUNTS_FILE", "TG_API_ID", "TG_API_HASH", "TG_SESSION"))
assert "A001" not in Path(os.environ["HAX_ACCOUNT_FILE"]).read_text()
fields = account_masks(account) + [os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]]
assert not any(name in os.environ for name in ("GITHUB_OUTPUT", "GITHUB_ENV", "GITHUB_STEP_SUMMARY", "GH_TOKEN"))
assert all(Path(os.environ[name]) == Path.cwd() for name in ("TMPDIR", "TMP", "TEMP"))
for value in fields + ["OTP_12345", "host.private.invalid", "2001:db8::1234"]:
    print(value)
    print(value, file=sys.stderr)
    os.write(1, value.encode())
    os.write(2, base64.b64encode(value.encode()))
    print(quote(value, safe=""))
thread = threading.Thread(target=lambda: print(fields[-1]))
thread.start(); thread.join()
subprocess.run([sys.executable, "-c", "import os; from pathlib import Path; print(Path(os.environ['HAX_ACCOUNT_FILE']).read_text())"], check=True)
Path("private.log").write_text(Path(os.environ["HAX_ACCOUNT_FILE"]).read_text())
Path(os.environ["TEST_LOCATION"]).write_text(str(Path.cwd()))
publish_progress("renewing")
sys.exit(publish_results([{"username": account["phone"], "servers": [{"old_valid_until": "2026-11-10", "info_valid_until": "2026-11-20", "success": True, "retry_count": 2}]}], "hax"))
""")
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_private(result)
        self.assertIn("account=2", result.stdout)
        self.assertIn("renewed=1", result.stdout)
        self.assertIn("retries=2", result.stdout)
        for value in ("OTP_12345", "host.private.invalid", "2001:db8::1234"):
            self.assertNotIn(value, result.stdout + result.stderr)
        self.assertFalse((self.root / "github-output").exists())
        self.assertFalse((self.root / "environment").exists())
        self.assertIn("续期成功 1 台", (self.root / "summary").read_text())
        self.assertEqual(
            (self.root / "hax-time-value.txt").read_text(),
            f"{state_key('+1-5550005678')},2026-11-20\n",
        )
        self.assertFalse(Path((self.root / "location").read_text()).exists())
        self.assertFalse(list(self.root.glob("*.log")))

    def test_import_error_and_missing_result_fail_without_traceback(self) -> None:
        for body in (
            'raise ImportError(Path(os.environ["HAX_ACCOUNT_FILE"]).read_text())',
            'print(Path(os.environ["HAX_ACCOUNT_FILE"]).read_text())',
        ):
            with self.subTest(body=body):
                self.worker(body)
                result = self.invoke()
                self.assertNotEqual(result.returncode, 0)
                self.assert_private(result)

    def test_results_distinguish_skipped_failed_unverified_and_no_vps(self) -> None:
        cases = [
            (
                {
                    "servers": [
                        {
                            "success": True,
                            "feedback_status": "success",
                            "submission_attempted": False,
                            "old_valid_until": "2026-11-20",
                        }
                    ]
                },
                1,
                "unverified=1",
                "2026-11-20",
            ),
            (
                {
                    "servers": [
                        {
                            "skipped": True,
                            "success": True,
                            "old_valid_until": "2026-12-01",
                        }
                    ]
                },
                0,
                "skipped=1",
                "2026-12-01",
            ),
            ({"servers": []}, 1, "failed=1", None),
            (
                {
                    "servers": [
                        {
                            "success": True,
                            "old_valid_until": "2026-11-10",
                            "new_valid_until": "2026-11-20",
                        }
                    ]
                },
                1,
                "unverified=1",
                "2026-11-10",
            ),
            (
                {
                    "servers": [
                        {
                            "success": True,
                            "feedback_status": "success",
                            "submission_attempted": True,
                            "old_valid_until": "2026-11-20",
                            "info_valid_until": "2026-11-20",
                        }
                    ]
                },
                0,
                "renewed=1",
                "2026-11-20",
            ),
            (
                {
                    "servers": [
                        {
                            "success": False,
                            "feedback_status": "failed",
                            "submission_attempted": True,
                            "old_valid_until": "2026-11-10",
                            "info_valid_until": "2026-11-20",
                        }
                    ]
                },
                1,
                "failed=1",
                "2026-11-20",
            ),
            (
                {
                    "servers": [
                        {
                            "success": False,
                            "feedback_status": "unknown",
                            "submission_attempted": True,
                            "old_valid_until": "2026-11-20",
                            "info_valid_until": "2026-11-20",
                        }
                    ]
                },
                1,
                "unverified=1",
                "2026-11-20",
            ),
            ({"servers": [{"success": False}]}, 1, "failed=1", None),
            ({"servers": [], "public_state": "no_vps"}, 0, "no_vps=1", "NOVPS"),
            ({"servers": [{"skipped": True}, {"success": False}]}, 1, "failed=1", None),
        ]
        for account, code, expected, state in cases:
            with self.subTest(expected=expected, account=account):
                account["username"] = "+1-5550001234"
                payload = repr(json.dumps([account]))
                self.worker(f'sys.exit(publish_results(json.loads({payload}), "hax"))')
                result = self.invoke()
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                self.assertIn(expected, result.stdout)
                self.assert_private(result)
                value_file = self.root / "hax-time-value.txt"
                if state:
                    self.assertEqual(
                        value_file.read_text(),
                        f"{state_key('+1-5550001234')},{state}\n",
                    )
                else:
                    self.assertFalse(value_file.exists())

    def test_report_rejects_free_text_extra_fields_and_invalid_counts(self) -> None:
        summary = dict.fromkeys(
            (
                "renewed",
                "skipped",
                "failed",
                "unverified",
                "no_vps",
                "retries",
                "warp_swaps",
                "warp_refresh",
            ),
            0,
        )
        summary["renewed"] = 1
        valid = {
            "version": 2,
            "stage": "complete",
            "summary": summary,
            "details": {
                "account_failures": 0,
                "text_sent": 0,
                "text_failed": 0,
                "photo_sent": 0,
                "photo_failed": 0,
                "reason": "none",
            },
        }
        cases = [
            {**valid, "secret": "FAKE_SESSION"},
            {**valid, "stage": "FAKE_SESSION"},
            {**valid, "summary": {**summary, "failed": "FAKE_SESSION"}},
            {**valid, "summary": {**summary, "renewed": True}},
            {**valid, "summary": {**summary, "renewed": 1_000_001}},
            {**valid, "summary": {**summary, "extra": 0}},
            {**valid, "details": {**valid["details"], "secret": "FAKE_SESSION"}},
            {**valid, "details": {**valid["details"], "reason": "FAKE_SESSION"}},
            {**valid, "details": {**valid["details"], "text_sent": True}},
            {**valid, "details": {**valid["details"], "account_failures": 1}},
        ]
        for report in cases:
            with self.subTest(report=report):
                self.worker(
                    f'Path(os.environ["PUBLIC_REPORT_FILE"]).write_text({repr(json.dumps(report))})'
                )
                result = self.invoke()
                self.assertNotEqual(result.returncode, 0)
                self.assert_private(result)
                self.assertNotIn("stage=complete", result.stdout)

    def test_invalid_configuration_is_not_echoed(self) -> None:
        self.env["BATCH_ACCOUNT_INDEX"] = "FAKE_SESSION"
        self.worker("raise AssertionError('must not start')")
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assert_private(result)

    def test_invalid_configuration_does_not_reuse_previous_state(self) -> None:
        self.worker("""
sys.exit(publish_results([{"username": "+1-5550001234", "servers": [], "public_state": "no_vps"}], "hax"))
""")
        initial = self.invoke()
        self.assertEqual(initial.returncode, 0, initial.stdout + initial.stderr)
        self.assertTrue((self.root / "hax-time-value.txt").exists())
        self.env["PUBLIC_RUN_TIMEOUT_SECONDS"] = "FAKE_SESSION"
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assert_private(result)
        self.assertFalse((self.root / "hax-time-value.txt").exists())

    def test_invalid_state_file_is_not_exported(self) -> None:
        self.worker("""
publish_results([{"username": "+1-5550001234", "servers": [], "public_state": "no_vps"}], "hax")
Path("hax-time-value.txt").write_text("FAKE_SESSION,bad-date")
""")
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assert_private(result)
        self.assertFalse((self.root / "hax-time-value.txt").exists())

    def test_nonzero_worker_exit_is_not_reported_as_success(self) -> None:
        self.worker("""
publish_results([{"username": "+1-5550001234", "servers": [], "public_state": "no_vps"}], "hax")
sys.exit(7)
""")
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("category=worker_error", result.stdout)
        self.assert_private(result)

    def test_woiden_uses_its_own_worker_and_state_file(self) -> None:
        self.worker(
            """
sys.exit(publish_results([{"username": "+1-5550001234", "servers": [], "public_state": "no_vps"}], "woiden"))
""",
            platform="woiden",
        )
        result = self.invoke("woiden")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("platform=woiden", result.stdout)
        self.assertFalse((self.root / "hax-time-value.txt").exists())
        self.assertTrue((self.root / "woiden-time-value.txt").exists())
        self.assert_private(result)

    def test_auth_check_preserves_expiry_state_and_normal_run_report(self) -> None:
        """登录检查通过不等于已续期，也不能改写续期结果和缓存。"""
        state = self.root / "hax-time-value.txt"
        state.write_text("existing-state")
        saved = self.root / "output/renewal-result.json"
        saved.parent.mkdir()
        saved.write_text("existing-result")
        self.worker("""
assert os.environ["HAX_AUTH_CHECK_ONLY"] == "1"
from scripts.public_report import authentication_result, record_check
publish_progress('preparing')
record_check('authentication', 'callback', 'ready')
Path(os.environ["PUBLIC_REPORT_FILE"]).write_text(json.dumps(authentication_result('valid', 'none')))
""")
        self.env["GITHUB_STEP_SUMMARY"] = str(self.root / "summary.md")
        result = subprocess.run(
            [sys.executable, "-m", "scripts.actions_runtime", "hax", "--check-auth"],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("result=authenticated", result.stdout)
        self.assert_private(result)
        self.assertEqual(state.read_text(), "existing-state")
        self.assertEqual(saved.read_text(), "existing-result")
        self.assertIn("本次未提交续期", (self.root / "summary.md").read_text())
        authentication = json.loads(
            (self.root / "output/authentication-result.json").read_text()
        )
        self.assertEqual(
            authentication["checks"],
            {"authentication": {"step": "callback", "result": "ready"}},
        )

    def test_authentication_failure_retains_client_and_peer_diagnostics(self) -> None:
        """只读失败保留实际子阶段，且不冒充会话失效或覆盖续期结果。"""
        for status in ("client_loading", "peer_loading"):
            with self.subTest(status=status):
                normal = self.root / "output/renewal-result.json"
                normal.parent.mkdir(exist_ok=True)
                normal.write_text("existing-result")
                self.worker(
                    "from scripts.public_report import authentication_result, record_check\n"
                    "publish_progress('preparing')\n"
                    f"record_check('authentication', 'telegram_web', {status!r})\n"
                    "Path(os.environ['PUBLIC_REPORT_FILE']).write_text(json.dumps(authentication_result('failed', 'telegram_peer_unverified')))\n"
                    "sys.exit(1)\n"
                )
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "scripts.actions_runtime",
                        "hax",
                        "--check-auth",
                    ],
                    cwd=self.root,
                    env=self.env,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                self.assertEqual(result.returncode, 1)
                authentication = json.loads(
                    (self.root / "output/authentication-result.json").read_text()
                )
                self.assertEqual(authentication["reason"], "telegram_peer_unverified")
                self.assertEqual(
                    authentication["checks"]["authentication"]["result"], status
                )
                self.assertIn(f"step=telegram_web result={status}", result.stdout)
                self.assertEqual(normal.read_text(), "existing-result")
                self.assert_private(result)

    def test_invalid_authentication_report_and_early_failure_replace_old_success(
        self,
    ) -> None:
        """不公开未校验诊断，也不把上轮只读成功带入本轮失败。"""
        saved = self.root / "output/authentication-result.json"
        saved.parent.mkdir()
        for checks in (
            {
                "authentication": {
                    "step": "telegram_web",
                    "result": "EXAMPLE_PRIVATE_VALUE",
                }
            },
            {"captcha": {"step": "turnstile", "result": "passed"}},
        ):
            with self.subTest(checks=checks):
                saved.write_text(
                    '{"version":1,"status":"valid","reason":"none","checks":{}}'
                )
                raw = dict(version=1, status="valid", reason="none", checks=checks)
                self.worker(
                    f"Path(os.environ['PUBLIC_REPORT_FILE']).write_text({json.dumps(raw)!r})\n"
                )
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "scripts.actions_runtime",
                        "hax",
                        "--check-auth",
                    ],
                    cwd=self.root,
                    env=self.env,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                self.assertNotEqual(result.returncode, 0)
                value = json.loads(saved.read_text())
                self.assertEqual(value["status"], "failed")
                self.assertEqual(value["reason"], "invalid_configuration_or_report")
                self.assertEqual(value["checks"], {})
                self.assertNotIn(
                    "EXAMPLE_PRIVATE_VALUE",
                    saved.read_text() + result.stdout + result.stderr,
                )
                self.assert_private(result)
        self.env.pop("RENEWAL_ACCOUNTS")
        result = subprocess.run(
            [sys.executable, "-m", "scripts.actions_runtime", "hax", "--check-auth"],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(saved.read_text())["status"], "failed")

    def test_hax_rejects_legacy_configuration_without_starting_worker(self) -> None:
        """缺少新配置时明确失败，不自动改用旧的授权流程。"""
        self.env.pop("RENEWAL_ACCOUNTS")
        marker = self.root / "started"
        self.worker(f"Path({str(marker)!r}).touch()")
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists())
        self.assert_private(result)

    def long_running_worker(self) -> Path:
        """建立另一个会话中的子进程，用于检查清理边界。"""
        marker = self.root / "child-pid"
        self.env["TEST_PID"] = str(marker)
        self.worker("""
import subprocess, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
Path(os.environ["TEST_PID"]).write_text(str(child.pid))
publish_progress("renewing")
time.sleep(60)
""")
        return marker

    def assert_stopped(self, marker: Path) -> None:
        """进程已退出或只剩等待系统回收的僵尸记录。"""
        pid = marker.read_text()
        status = subprocess.run(
            ["ps", "-p", pid, "-o", "stat="],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertTrue(
            not status.stdout.strip() or status.stdout.strip().startswith("Z"),
            status.stdout,
        )

    def test_timeout_stops_detached_child(self) -> None:
        marker = self.long_running_worker()
        self.env["PUBLIC_RUN_TIMEOUT_SECONDS"] = "1"
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("category=timeout", result.stdout)
        self.assert_private(result)
        self.assert_stopped(marker)

    def test_cancel_stops_detached_child(self) -> None:
        marker = self.long_running_worker()
        with subprocess.Popen(
            [sys.executable, "-m", "scripts.actions_runtime", "hax"],
            cwd=self.root,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as process:
            deadline = time.monotonic() + 5
            while (
                not marker.exists()
                and process.poll() is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.02)
            self.assertTrue(marker.exists())
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 130, stdout + stderr)
            self.assertIn("category=cancelled", stdout)
        self.assert_stopped(marker)


if __name__ == "__main__":
    unittest.main()

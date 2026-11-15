"""从正式 CLI 验证公开输出、结果状态与进程清理。"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from scripts.accounts import state_key

PROJECT = Path(__file__).resolve().parents[1]
ACCOUNT = (
    "+1-5550001234,123456:FAKE_BOT_TOKEN,chat-9988,654321,FAKE_API_HASH,FAKE_SESSION"
)
SECOND = (
    "+1-5550005678,987654:OTHER_BOT_TOKEN,chat-8877,765432,OTHER_API_HASH,OTHER_SESSION"
)


def environment() -> dict[str, str]:
    """测试环境不继承真实凭据或 GitHub 的写入通道。"""
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("GITHUB_", "ACTIONS_", "PUBLIC_", "TG_", "GIT_CONFIG_"))
        and key
        not in {
            "GH_TOKEN",
            "BATCH",
            "PROXY_CONTENT",
            "TWOCAPTCHA_API_KEY",
            "PYTHONPATH",
        }
    }


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
        self.env.update({"BATCH": ACCOUNT, "PUBLIC_HEARTBEAT_SECONDS": "1"})

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
        self.assertNotIn("Traceback", output)

    def test_raw_streams_threads_subprocesses_and_files_stay_private(self) -> None:
        self.env.update(
            {
                "BATCH": ACCOUNT + ";" + SECOND,
                "BATCH_ACCOUNT_INDEX": "2",
                "GITHUB_OUTPUT": str(self.root / "output"),
                "GITHUB_ENV": str(self.root / "environment"),
                "GITHUB_STEP_SUMMARY": str(self.root / "summary"),
                "TEST_LOCATION": str(self.root / "location"),
            }
        )
        self.worker("""
import base64, subprocess, threading
from urllib.parse import quote
fields = os.environ["BATCH"].split(",")
assert len(fields) == 6 and fields[0] == "+1-5550005678"
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
subprocess.run([sys.executable, "-c", "import os; print(os.environ['BATCH'])"], check=True)
Path("private.log").write_text(os.environ["BATCH"])
Path(os.environ["TEST_LOCATION"]).write_text(str(Path.cwd()))
publish_progress("renewing")
sys.exit(publish_results([{"username": fields[0], "servers": [{"old_valid_until": "2026-11-10", "info_valid_until": "2026-11-20", "success": True, "retry_count": 2}]}], "hax"))
""")
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_private(result)
        self.assertIn("account=2", result.stdout)
        self.assertIn("renewed=1", result.stdout)
        self.assertIn("retries=2", result.stdout)
        for value in ("OTP_12345", "host.private.invalid", "2001:db8::1234"):
            self.assertNotIn(value, result.stdout + result.stderr)
        self.assertFalse((self.root / "output").exists())
        self.assertFalse((self.root / "environment").exists())
        self.assertIn("renewed", (self.root / "summary").read_text())
        self.assertEqual(
            (self.root / "hax-time-value.txt").read_text(),
            f"{state_key('+1-5550005678')},2026-11-20\n",
        )
        self.assertFalse(Path((self.root / "location").read_text()).exists())
        self.assertFalse(list(self.root.glob("*.log")))

    def test_import_error_and_missing_result_fail_without_traceback(self) -> None:
        for body in (
            'raise ImportError(os.environ["BATCH"])',
            'print(os.environ["BATCH"])',
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
        valid = {"version": 1, "stage": "complete", "summary": summary}
        cases = [
            {**valid, "secret": "FAKE_SESSION"},
            {**valid, "stage": "FAKE_SESSION"},
            {**valid, "summary": {**summary, "failed": "FAKE_SESSION"}},
            {**valid, "summary": {**summary, "renewed": True}},
            {**valid, "summary": {**summary, "renewed": 1_000_001}},
            {**valid, "summary": {**summary, "extra": 0}},
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

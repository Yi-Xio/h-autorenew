"""从正式 CLI 验证文件预筛、通知和公开输出边界。"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from scripts.accounts import public_account_id, register_masks, state_key
from scripts.workflow import notify_skipped
from tests.test_runtime import ACCOUNT, PROJECT, SECOND, environment


class WorkflowTests(unittest.TestCase):
    """只使用假账号，通知和 WARP 均由替身提供。"""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="hax-test-workflow-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        shutil.copytree(
            PROJECT / "scripts",
            self.root / "scripts",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        self.env = environment()
        self.env.update(
            {
                "BATCH": ACCOUNT + ";" + SECOND,
                "BATCH_ACCOUNT_INDEX": "1",
                "GITHUB_OUTPUT": str(self.root / "output.txt"),
                "GITHUB_STEP_SUMMARY": str(self.root / "summary.md"),
                "GITHUB_RUN_ID": "100",
                "GITHUB_RUN_ATTEMPT": "1",
            }
        )

    def invoke(
        self, command: str = "check", platform: str = "hax"
    ) -> subprocess.CompletedProcess[str]:
        """从正式入口观察输出，每个步骤使用独立的 GITHUB_OUTPUT 文件。"""
        (self.root / "output.txt").unlink(missing_ok=True)
        result = subprocess.run(
            [sys.executable, "-m", "scripts.workflow", command, platform],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assert_private(result.stdout + result.stderr)
        if (self.root / "output.txt").exists():
            self.assert_private((self.root / "output.txt").read_text())
        return result

    def outputs(self) -> dict[str, str]:
        """读取步骤交给官方 Action 的受控输出。"""
        return dict(
            line.split("=", 1)
            for line in (self.root / "output.txt").read_text().splitlines()
        )

    def assert_private(self, text: str) -> None:
        """日志、输出和缓存中不能出现假凭据或原手机号索引。"""
        for secret in (ACCOUNT + "," + SECOND).split(","):
            self.assertNotIn(secret, text)
        for account in (ACCOUNT, SECOND):
            self.assertNotIn(state_key(account.split(",")[0]), text)
        self.assertNotIn("Traceback", text)

    def test_matrix_only_emits_numeric_indexes(self) -> None:
        result = self.invoke("matrix")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.outputs()["matrix"]), {"index": [1, 2]})

    def test_file_precheck_uses_own_identity_platform_threshold_and_reordering(
        self,
    ) -> None:
        today = datetime.now(timezone(timedelta(hours=8))).date()
        for platform, threshold in (("hax", 5), ("woiden", 2)):
            for days in (threshold - 1, threshold):
                with self.subTest(platform=platform, days=days):
                    key = public_account_id(platform, ACCOUNT.split(","))
                    date = (today + timedelta(days=days)).isoformat()
                    (self.root / f"{platform}-time.txt").write_text(f"{key},{date}\n")
                    self.env.update(
                        BATCH=SECOND + ";" + ACCOUNT, BATCH_ACCOUNT_INDEX="2"
                    )
                    self.assertEqual(self.invoke(platform=platform).returncode, 0)
                    self.assertEqual(self.outputs()["repository_status"], "hit")
                    self.assertEqual(
                        self.outputs()["should_run"],
                        "true" if days < threshold else "false",
                    )

    def test_missing_legacy_or_invalid_file_and_no_vps_trigger_website_check(
        self,
    ) -> None:
        key = public_account_id("hax", ACCOUNT.split(","))
        path = self.root / "hax-time.txt"
        self.assertEqual(self.invoke().returncode, 0)
        self.assertEqual(self.outputs()["should_run"], "true")
        for text in (
            f"{state_key(ACCOUNT.split(',')[0])},2099-12-31\n",
            f"{key},NOVPS\n",
            "FAKE_SESSION,bad-date",
            "x" * 65537,
        ):
            with self.subTest(text=text):
                path.write_text(text)
                self.assertEqual(self.invoke().returncode, 0)
                self.assertEqual(self.outputs()["should_run"], "true")

    def test_state_command_does_not_echo_raw_git_failure_or_write_without_result(
        self,
    ) -> None:
        self.assertEqual(self.invoke("save-state").returncode, 0)
        self.assertEqual(self.outputs()["state_status"], "no_observed_state")
        (self.root / "hax-time-value.txt").write_text(
            f"{state_key(ACCOUNT.split(',')[0])},2026-12-01\n"
        )
        binary = self.root / "bin"
        binary.mkdir()
        git = binary / "git"
        git.write_text("#!/bin/sh\necho FAKE_SESSION\necho FAKE_API_HASH >&2\nexit 1\n")
        git.chmod(0o700)
        self.env["PATH"] = str(binary) + os.pathsep + self.env["PATH"]
        self.assertNotEqual(self.invoke("save-state").returncode, 0)
        self.assertFalse((self.root / "output.txt").exists())

    def test_missing_batch_and_non_runtime_branches_fail_without_publishing_secrets(
        self,
    ) -> None:
        self.env["BATCH"] = ""
        self.assertNotEqual(self.invoke("matrix").returncode, 0)
        self.assertFalse((self.root / "output.txt").exists())
        self.env["BATCH"] = ACCOUNT
        self.env["GITHUB_ACTIONS"] = "true"
        for ref in ("refs/heads/upstream", "refs/tags/master"):
            self.env["GITHUB_REF"] = ref
            for command in (
                "matrix",
                "check",
                "notify-skipped",
                "save-state",
            ):
                with self.subTest(ref=ref, command=command):
                    self.assertNotEqual(self.invoke(command).returncode, 0)
                    self.assertFalse((self.root / "output.txt").exists())

    def test_notification_checks_api_result_and_only_uses_selected_account(
        self,
    ) -> None:
        output = io.StringIO()
        with (
            patch.dict(
                os.environ,
                {
                    "BATCH": ACCOUNT + ";" + SECOND,
                    "DAYS_LEFT": "6",
                    "GITHUB_ACTIONS": "false",
                },
            ),
            patch(
                "scripts.workflow.urlopen",
                side_effect=[
                    io.BytesIO(b'{"ok": false, "description": "FAKE_SESSION"}'),
                    io.BytesIO(b'{"ok": true}'),
                ],
            ) as request,
            contextlib.redirect_stdout(output),
        ):
            for index in (1, 2):
                with patch.dict(os.environ, {"BATCH_ACCOUNT_INDEX": str(index)}):
                    notify_skipped("hax")
        self.assertIn("account=1 stage=notification result=failed", output.getvalue())
        self.assertIn("account=2 stage=notification result=sent", output.getvalue())
        for call, account in zip(
            request.call_args_list, (ACCOUNT, SECOND), strict=True
        ):
            fields = account.split(",")
            self.assertEqual(
                call.args[0].full_url,
                f"https://api.telegram.org/bot{fields[1]}/sendMessage",
            )
            self.assertIn(fields[2].encode(), call.args[0].data)
        self.assert_private(output.getvalue())

    def test_notification_network_failure_does_not_echo_request_url(self) -> None:
        output = io.StringIO()
        with (
            patch.dict(
                os.environ,
                {
                    "BATCH": ACCOUNT,
                    "BATCH_ACCOUNT_INDEX": "1",
                    "DAYS_LEFT": "6",
                    "GITHUB_ACTIONS": "false",
                },
            ),
            patch(
                "scripts.workflow.urlopen",
                side_effect=URLError(
                    "https://api.telegram.org/bot123456:FAKE_BOT_TOKEN/sendMessage"
                ),
            ),
            contextlib.redirect_stdout(output),
        ):
            notify_skipped("hax")
        self.assertIn("category=notification_failed", output.getvalue())
        self.assertNotIn("https://", output.getvalue())
        self.assert_private(output.getvalue())

    def test_mask_registration_escapes_workflow_commands(self) -> None:
        output = io.StringIO()
        with (
            patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}),
            contextlib.redirect_stdout(output),
        ):
            register_masks(["FAKE%VALUE\r\n::error::injected"])
        self.assertEqual(
            output.getvalue(), "::add-mask::FAKE%25VALUE%0D%0A::error::injected\n"
        )
        output = io.StringIO()
        with (
            patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}),
            contextlib.redirect_stdout(output),
        ):
            register_masks(["FAKE_SESSION"])
        self.assertEqual(output.getvalue(), "")

    def test_warp_setup_never_executes_host_commands_or_publishes_raw_output(
        self,
    ) -> None:
        binary = self.root / "bin"
        binary.mkdir()
        stubs = {
            "sudo": '#!/bin/sh\necho FAKE_SESSION\nif [ "${FAIL_INSTALL:-0}" = 1 ]; then exit 1; fi\ncat >/dev/null\n',
            "curl": '#!/bin/sh\necho "ip=2001:db8::1234"\ncase "$*" in *cdn-cgi/trace*)\n  if [ "${DUAL_STACK:-0}" = 1 ]; then\n    case " $* " in *" -4 "*) echo "warp=on";; *) echo "warp=off";; esac\n  else\n    echo "warp=${WARP_STATE:-on}"\n  fi\n;; esac\n',
            "lsb_release": "#!/bin/sh\necho noble\n",
            "sleep": "#!/bin/sh\nexit 0\n",
        }
        for name, content in stubs.items():
            file = binary / name
            file.write_text(content)
            file.chmod(0o700)
        config = {
            **self.env,
            "PATH": str(binary) + os.pathsep + self.env.get("PATH", ""),
        }
        for extra, expected in (
            ({}, 0),
            ({"FAIL_INSTALL": "1"}, 1),
            ({"WARP_STATE": "off"}, 1),
            ({"DUAL_STACK": "1"}, 0),
        ):
            with self.subTest(config=extra):
                result = subprocess.run(
                    ["/bin/bash", str(PROJECT / "scripts/setup_warp.sh")],
                    env={**config, **extra},
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(
                    result.returncode, expected, result.stdout + result.stderr
                )
                self.assertNotIn("FAKE_SESSION", result.stdout + result.stderr)
                self.assertNotIn("2001:db8", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()

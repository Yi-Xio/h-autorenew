"""通过正式 CLI 验证账号缓存与续期衔接，网络和 WARP 均用替身。"""

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

from scripts.accounts import register_masks, state_key
from scripts.state import CACHE_FILE, prepare_cached_state
from scripts.workflow import notify_skipped
from tests.test_runtime import ACCOUNT, PROJECT, SECOND, environment


class WorkflowTests(unittest.TestCase):
    """只注入假账号，缓存传输由临时文件模拟，不发送真实通知。"""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="hax-test-workflow-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        shutil.copytree(
            PROJECT / "scripts",
            self.root / "scripts",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        self.cache = self.root / CACHE_FILE
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

    def cached(self, state: str, platform: str = "hax", account: str = ACCOUNT) -> None:
        """生成经过校验的测试快照，随后由正式 CLI 恢复使用。"""
        observed = self.root / "fixture.txt"
        observed.write_text(f"{state_key(account.split(',')[0])},{state}\n")
        prepare_cached_state(self.cache, observed, platform, account.split(","))
        observed.unlink()

    def test_matrix_only_emits_numeric_indexes(self) -> None:
        result = self.invoke("matrix")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.outputs()["matrix"]), {"index": [1, 2]})

    def test_cache_miss_ignores_upstream_repository_records(self) -> None:
        (self.root / "hax-time.txt").write_text(
            f"{state_key(ACCOUNT.split(',')[0])},2099-12-31\n"
        )
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.outputs()["should_run"], "true")
        self.assertEqual(self.outputs()["cache_status"], "miss")

    def test_precheck_uses_platform_threshold_and_valid_prefix_restore(self) -> None:
        today = datetime.now(timezone(timedelta(hours=8))).date()
        for platform, threshold in (("hax", 5), ("woiden", 2)):
            for days in (threshold - 1, threshold):
                with self.subTest(platform=platform, days=days):
                    self.cached((today + timedelta(days=days)).isoformat(), platform)
                    result = self.invoke(platform=platform)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(self.outputs()["cache_status"], "hit")
                    self.assertEqual(
                        self.outputs()["should_run"],
                        "true" if days < threshold else "false",
                    )
                    self.assertEqual(self.outputs()["days_left"], str(days))

    def test_damaged_or_failed_restore_forces_site_check_without_echoing_input(
        self,
    ) -> None:
        self.cache.parent.mkdir(parents=True)
        for body in ("FAKE_SESSION,bad-date", '{"version": true}', "x" * 4097):
            with self.subTest(body=body):
                self.cache.write_text(body)
                result = self.invoke()
                self.assertEqual(result.returncode, 0)
                self.assertEqual(self.outputs()["should_run"], "true")
                self.assertEqual(self.outputs()["cache_status"], "invalid")
        self.cached("2099-12-31")
        self.env["CACHE_RESTORE_OUTCOME"] = "failure"
        self.assertEqual(self.invoke().returncode, 0)
        self.assertEqual(self.outputs()["should_run"], "true")
        self.assertEqual(self.outputs()["cache_status"], "restore_failed")

    def test_no_vps_state_still_runs_to_check_current_site_status(self) -> None:
        self.cached("NOVPS")
        self.assertEqual(self.invoke().returncode, 0)
        self.assertEqual(self.outputs()["should_run"], "true")
        self.assertEqual(self.outputs()["cache_status"], "hit")

    def test_cache_keys_follow_account_after_reordering_and_change_for_reruns(
        self,
    ) -> None:
        self.cache.parent.mkdir(parents=True)
        self.cache.write_text("FAKE_SESSION")
        self.cache.with_suffix(".tmp").write_text("FAKE_SESSION")
        self.assertEqual(self.invoke("setup-cache").returncode, 0)
        first = self.outputs()
        self.assertFalse(self.cache.exists())
        self.assertFalse(self.cache.with_suffix(".tmp").exists())
        self.env["BATCH"] = SECOND + ";" + ACCOUNT
        self.env["BATCH_ACCOUNT_INDEX"] = "2"
        self.env["GITHUB_RUN_ATTEMPT"] = "2"
        self.assertEqual(self.invoke("setup-cache").returncode, 0)
        second = self.outputs()
        self.assertEqual(first["cache_prefix"], second["cache_prefix"])
        self.assertNotEqual(first["cache_key"], second["cache_key"])
        self.env["BATCH_ACCOUNT_INDEX"] = "1"
        self.assertEqual(self.invoke("setup-cache").returncode, 0)
        self.assertNotEqual(first["cache_prefix"], self.outputs()["cache_prefix"])

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
                "setup-cache",
                "check",
                "notify-skipped",
                "prepare-state",
            ):
                with self.subTest(ref=ref, command=command):
                    self.assertNotEqual(self.invoke(command).returncode, 0)
                    self.assertFalse((self.root / "output.txt").exists())

    def test_prepare_only_saves_reliable_current_account_results(self) -> None:
        self.cached("2026-12-01")
        original = self.cache.read_bytes()
        result = self.invoke("prepare-state")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            self.outputs(), {"changed": "false", "state_status": "no_observed_state"}
        )
        self.assertEqual(self.cache.read_bytes(), original)
        observed = self.root / "hax-time-value.txt"
        observed.write_text(f"{state_key(ACCOUNT.split(',')[0])},2026-12-01\n")
        self.assertEqual(self.invoke("prepare-state").returncode, 0)
        self.assertEqual(self.outputs()["state_status"], "unchanged")
        observed.write_text(f"{state_key(SECOND.split(',')[0])},2026-12-02\n")
        self.assertNotEqual(self.invoke("prepare-state").returncode, 0)
        self.assertEqual(self.cache.read_bytes(), original)
        self.assertFalse((self.root / ".git").exists())

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

    def test_round_trip_separates_accounts_and_platforms_and_keeps_access_on_skip(
        self,
    ) -> None:
        today = datetime.now(timezone(timedelta(hours=8))).date()
        old = today.isoformat()
        future = (today + timedelta(days=7)).isoformat()
        worker = (
            "import os, sys\nfrom scripts.public_report import publish_results\nfields = os.environ['BATCH'].split(',')\nassert len(fields) == 6\nsys.exit(publish_results([{'username': fields[0], 'servers': [{'old_valid_until': '"
            + old
            + "', 'info_valid_until': '"
            + future
            + "', 'success': True}]}], PLATFORM))\n"
        )
        for platform in ("hax", "woiden"):
            (self.root / f"{platform}-renew.py").write_text(
                worker.replace("PLATFORM", repr(platform))
            )
        remote = {}
        restores = 0
        for cycle in (1, 2):
            self.env["GITHUB_RUN_ID"] = str(100 + cycle)
            self.env["BATCH"] = (
                ACCOUNT + ";" + SECOND if cycle == 1 else SECOND + ";" + ACCOUNT
            )
            self.assertEqual(self.invoke("matrix").returncode, 0)
            indexes = json.loads(self.outputs()["matrix"])["index"]
            for platform in ("hax", "woiden"):
                for index in indexes:
                    self.env["BATCH_ACCOUNT_INDEX"] = str(index)
                    self.assertEqual(self.invoke("setup-cache", platform).returncode, 0)
                    keys = self.outputs()
                    prefix = keys["cache_prefix"]
                    if prefix in remote:
                        self.cache.write_bytes(remote[prefix])
                    restores += 1
                    self.assertEqual(self.invoke(platform=platform).returncode, 0)
                    if cycle == 2:
                        self.assertEqual(self.outputs()["should_run"], "false")
                        self.assertEqual(self.outputs()["cache_status"], "hit")
                        continue
                    self.assertEqual(self.outputs()["should_run"], "true")
                    result = subprocess.run(
                        [sys.executable, "-m", "scripts.actions_runtime", platform],
                        cwd=self.root,
                        env=self.env,
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                    self.assertEqual(
                        result.returncode, 0, result.stdout + result.stderr
                    )
                    self.assert_private(result.stdout + result.stderr)
                    self.assertEqual(
                        self.invoke("prepare-state", platform).returncode, 0
                    )
                    self.assertEqual(self.outputs()["changed"], "true")
                    self.assert_private(self.cache.read_text())
                    remote[prefix] = self.cache.read_bytes()
        self.assertEqual(restores, 8)
        self.assertEqual(len(remote), 4)
        self.assertFalse((self.root / ".git").exists())

    def test_failed_cache_upload_can_restore_previous_remote_snapshot(self) -> None:
        today = datetime.now(timezone(timedelta(hours=8))).date()
        self.cached(today.isoformat())
        remote = self.cache.read_bytes()
        (self.root / "hax-time-value.txt").write_text(
            f"{state_key(ACCOUNT.split(',')[0])},{(today + timedelta(days=7)).isoformat()}\n"
        )
        self.assertEqual(self.invoke("prepare-state").returncode, 0)
        self.assertEqual(self.outputs()["changed"], "true")
        # 模拟上传失败，远端仍保留原快照，下一轮恢复后重新检查网站。
        self.assertEqual(self.invoke("setup-cache").returncode, 0)
        self.cache.write_bytes(remote)
        self.assertEqual(self.invoke().returncode, 0)
        self.assertEqual(self.outputs()["should_run"], "true")

    def test_cache_miss_starts_worker_but_site_date_can_still_skip_renewal(
        self,
    ) -> None:
        future = (
            datetime.now(timezone(timedelta(hours=8))).date() + timedelta(days=7)
        ).isoformat()
        self.assertEqual(self.invoke().returncode, 0)
        self.assertEqual(self.outputs()["should_run"], "true")
        self.assertEqual(self.outputs()["cache_status"], "miss")
        (self.root / "hax-renew.py").write_text(
            "import os, sys\n"
            "from scripts.public_report import publish_results\n"
            "phone = os.environ['BATCH'].split(',')[0]\n"
            f"sys.exit(publish_results([{{'username': phone, 'servers': [{{'old_valid_until': '{future}', 'skipped': True}}]}}], 'hax'))\n"
        )
        result = subprocess.run(
            [sys.executable, "-m", "scripts.actions_runtime", "hax"],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_private(result.stdout + result.stderr)
        self.assertIn("renewed=0 skipped=1", result.stdout)
        self.assertEqual(self.invoke("prepare-state").returncode, 0)
        self.assertEqual(self.outputs()["changed"], "true")
        self.assertEqual(self.invoke().returncode, 0)
        self.assertEqual(self.outputs()["should_run"], "false")

    def test_failed_renewal_can_still_save_an_observed_date(self) -> None:
        old = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        (self.root / "hax-renew.py").write_text(
            "import os, sys\n"
            "from scripts.public_report import publish_results\n"
            "phone = os.environ['BATCH'].split(',')[0]\n"
            f"sys.exit(publish_results([{{'username': phone, 'servers': [{{'old_valid_until': '{old}', 'success': False}}]}}], 'hax'))\n"
        )
        result = subprocess.run(
            [sys.executable, "-m", "scripts.actions_runtime", "hax"],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assert_private(result.stdout + result.stderr)
        self.assertEqual(self.invoke("prepare-state").returncode, 0)
        self.assertEqual(self.outputs()["changed"], "true")
        self.assertEqual(json.loads(self.cache.read_text())["state"], old)

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

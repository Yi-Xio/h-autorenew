"""从正式入口验证中文摘要、状态分离和公开字段校验。"""

import base64
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.job_summary import RUNTIME_FILE, empty_details, save_runtime
from scripts.public_report import COUNTERS
from tests.test_runtime import ACCOUNT, PROJECT, environment


class SummaryTests(unittest.TestCase):
    """不使用真实凭据、浏览器或缓存服务。"""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="hax-test-summary-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        shutil.copytree(
            PROJECT / "scripts",
            self.root / "scripts",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        self.summary_file = self.root / "summary.md"
        self.env = {
            **environment(),
            "BATCH_ACCOUNT_INDEX": "1",
            "GITHUB_STEP_SUMMARY": str(self.summary_file),
            "CHECK_OUTCOME": "success",
            "SHOULD_RUN": "true",
            "CACHE_STATUS": "hit",
            "RENEW_OUTCOME": "success",
        }

    def runtime(self, **changes: object) -> None:
        """使用受控入口的公共写入协议，模拟已取得的可靠结果。"""
        counts: dict[str, int] = dict.fromkeys(COUNTERS, 0)
        counts["renewed"] = 1
        details = empty_details()
        details["text_sent"] = 1
        value: dict[str, object] = dict(
            version=1,
            platform="hax",
            index=1,
            result="complete",
            stage="complete",
            reason="none",
            elapsed_seconds=125,
            summary=counts,
            details=details,
        )
        value.update(changes)
        save_runtime(self.root, value)

    def invoke(self) -> str:
        self.summary_file.unlink(missing_ok=True)
        result = subprocess.run(
            [sys.executable, "-m", "scripts.workflow", "summary", "hax"],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.summary_file.exists())
        content = self.summary_file.read_text()
        for secret in ACCOUNT.split(",") + [
            "FAKE_MARKDOWN",
            "host.private.invalid",
            "2001:db8::1234",
        ]:
            for value in (secret, base64.b64encode(secret.encode()).decode()):
                self.assertNotIn(value, content + result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        return content

    def test_success_uses_one_compact_chinese_table_and_collapsed_details(self) -> None:
        self.runtime()
        self.env.update(
            STATE_STATUS="changed",
            SAVE_OUTCOME="success",
            VERIFY_OUTCOME="success",
            VERIFY_HIT="true",
        )
        content = self.invoke()
        for value in (
            "Hax · 账号 #1",
            "已验证延长",
            "已延长 1 台",
            "已发送 1 条",
            "未生成可发送截图",
            "已保存（已确认本轮缓存存在）",
            "2 分 5 秒",
            "<details>",
            "</details>",
        ):
            self.assertIn(value, content)
        self.assertEqual(content.count("### "), 1)
        self.assertLess(len(content.encode()), 16384)

    def test_warp_failures_have_specific_reason_and_location(self) -> None:
        for reason, label, stage in (
            ("warp_registration_failed", "WARP 注册失败", "注册 WARP"),
            ("warp_connection_failed", "未能核实 WARP 连接", "核实 WARP 连接"),
            ("warp_ip_query_failed", "未取得有效的 WARP 出口 IP", "查询 WARP 出口"),
            ("warp_new_exit_unavailable", "未取得新的 WARP 出口", "更换 WARP 出口"),
        ):
            counts = dict.fromkeys(COUNTERS, 0)
            counts["failed"] = 1
            self.runtime(
                result="failed",
                reason=reason,
                summary=counts,
                details=dict(empty_details(), reason=reason),
            )
            content = self.invoke()
            self.assertIn(label, content)
            self.assertIn("| 失败或待核实位置 | " + stage + " |", content)

    def test_notification_failure_does_not_rewrite_verified_renewal(self) -> None:
        details = {**empty_details(), "text_failed": 1, "photo_sent": 1}
        self.runtime(details=details)
        content = self.invoke()
        self.assertIn("已验证延长", content)
        self.assertIn("Telegram 文字 | 失败 1 条", content)
        self.assertIn("Telegram 截图 | 已发送 1 张", content)
        self.assertIn("通知未全部发送成功", content)
        self.assertNotIn("续期失败 1 台", content)

    def test_all_skipped_has_summary_without_worker_or_zero_vps_claim(self) -> None:
        self.env.update(SHOULD_RUN="false", DAYS_LEFT="7", NOTIFY_STATUS="failed")
        content = self.invoke()
        self.assertIn("无需续期", content)
        self.assertIn("未启动续期脚本", content)
        self.assertIn("剩余 7 天", content)
        self.assertIn("发送失败 1 条", content)
        self.assertIn("无需更新", content)
        self.assertNotIn("无可用 VPS", content)
        self.assertNotIn("0 台", content)
        self.env.update(NOTIFY_STATUS="", NOTIFY_OUTCOME="failure")
        self.assertIn("通知步骤失败", self.invoke())

    def test_account_failure_is_not_counted_as_a_failed_vps(self) -> None:
        counts: dict[str, int] = dict.fromkeys(COUNTERS, 0)
        counts["failed"] = 1
        self.runtime(
            result="failed",
            summary=counts,
            reason="login_failed",
            details={
                **empty_details(),
                "account_failures": 1,
                "reason": "login_failed",
            },
        )
        content = self.invoke()
        self.assertIn("账号检查失败 1 个", content)
        self.assertIn("网站登录失败", content)
        self.assertNotIn("续期失败 1 台", content)

    def test_no_vps_and_unverified_outcomes_use_correct_units(self) -> None:
        for key, expected in (
            ("no_vps", "无可用 VPS 1 个账号"),
            ("unverified", "待核实 1 台"),
            ("failed", "续期失败 1 台"),
            ("skipped", "无需续期 1 台"),
        ):
            with self.subTest(result=key):
                counts: dict[str, int] = dict.fromkeys(COUNTERS, 0)
                counts[key] = 1
                self.runtime(
                    summary=counts,
                    result="failed" if key in {"unverified", "failed"} else "complete",
                )
                self.assertIn(expected, self.invoke())

    def test_cache_preparation_and_green_save_are_not_confirmation(self) -> None:
        self.runtime()
        cases = [
            (dict(STATE_STATUS="changed", SAVE_OUTCOME="success"), "待确认"),
            (
                dict(
                    STATE_STATUS="changed",
                    SAVE_OUTCOME="success",
                    VERIFY_OUTCOME="failure",
                    VERIFY_HIT="true",
                ),
                "待确认",
            ),
            (
                dict(
                    STATE_STATUS="changed",
                    SAVE_OUTCOME="success",
                    VERIFY_OUTCOME="success",
                    VERIFY_HIT="false",
                ),
                "待确认",
            ),
            (dict(STATE_STATUS="changed", SAVE_OUTCOME="failure"), "保存失败"),
            (
                dict(
                    STATE_STATUS="changed",
                    SAVE_OUTCOME="success",
                    VERIFY_OUTCOME="success",
                    VERIFY_HIT="true",
                ),
                "已保存（已确认本轮缓存存在）",
            ),
            (dict(STATE_STATUS="unchanged"), "无需更新"),
            (dict(STATE_STATUS="no_observed_state"), "无可靠新值，保留旧记录"),
            (dict(STATE_OUTCOME="failure"), "状态准备失败"),
        ]
        for values, expected in cases:
            with self.subTest(values=values):
                previous = dict(self.env)
                self.env.update(values)
                content = self.invoke()
                self.assertIn(expected, content)
                if expected != "已保存（已确认本轮缓存存在）":
                    self.assertNotIn("| 到期日保存 | 已保存", content)
                self.env = previous
        self.env.update(CACHE_STATUS="miss", STATE_STATUS="no_observed_state")
        self.assertIn("无可靠新值，未保存", self.invoke())

    def test_early_failures_timeout_and_cancellation_have_useful_summary(self) -> None:
        self.env.update(WARP_OUTCOME="failure")
        content = self.invoke()
        self.assertIn("启动失败", content)
        self.assertIn("WARP 连接失败", content)
        self.assertIn("连接 WARP", content)
        for reason, expected in (
            ("timeout", "运行超时"),
            ("cancelled", "执行已取消"),
            ("worker_error", "进程异常退出"),
            ("renewal_code_unavailable", "续期验证码"),
            ("invalid_configuration_or_report", "未通过校验"),
        ):
            with self.subTest(reason=reason):
                self.runtime(
                    result="cancelled" if reason == "cancelled" else "failed",
                    summary=None,
                    reason=reason,
                    stage="login",
                )
                content = self.invoke()
                self.assertIn(expected, content)
                self.assertIn("登录网站", content)
                self.assertNotIn("已验证延长", content)

    def test_skipped_worker_does_not_reuse_a_previous_success_report(self) -> None:
        self.runtime()
        self.env.update(RENEW_OUTCOME="skipped", WARP_OUTCOME="failure")
        content = self.invoke()
        self.assertIn("WARP 连接失败", content)
        self.assertNotIn("已验证延长", content)

    def test_extra_fields_free_text_wrong_identity_and_bad_counts_are_not_rendered(
        self,
    ) -> None:
        self.runtime()
        path = self.root / RUNTIME_FILE
        good = json.loads(path.read_text())
        bad = [
            {**good, "extra": "FAKE_MARKDOWN"},
            {**good, "reason": "FAKE_MARKDOWN"},
            {**good, "platform": "woiden"},
            {**good, "index": 2},
            {**good, "elapsed_seconds": True},
            {**good, "details": {**good["details"], "text_sent": True}},
            {**good, "summary": {**good["summary"], "renewed": "FAKE_MARKDOWN"}},
            {
                **good,
                "summary": {**good["summary"], "failed": 0},
                "details": {**good["details"], "account_failures": 1},
            },
        ]
        for value in bad:
            with self.subTest(value=value):
                path.write_text(json.dumps(value))
                content = self.invoke()
                self.assertIn("运行报告未通过校验", content)
                self.assertNotIn("已验证延长", content)
        path.write_text("FAKE_MARKDOWN" * 2000)
        self.assertIn("运行报告未通过校验", self.invoke())
        path.unlink()
        self.env.update(
            DAYS_LEFT="FAKE_MARKDOWN",
            CACHE_STATUS="FAKE_MARKDOWN",
            NOTIFY_STATUS="FAKE_MARKDOWN",
            STATE_STATUS="FAKE_MARKDOWN",
            SHOULD_RUN="false",
        )
        self.invoke()

    def test_runtime_defers_summary_until_workflow_has_cache_results(self) -> None:
        self.env.update(
            BATCH=ACCOUNT,
            PUBLIC_SUMMARY_DEFERRED="1",
            CACHE_STATUS="miss",
            GITHUB_OUTPUT=str(self.root / "step.txt"),
        )
        (self.root / "hax-renew.py").write_text(
            "import os, sys\nfrom scripts.public_report import publish_results, record_notification\n"
            "record_notification('text', True)\n"
            "sys.exit(publish_results([{'username': os.environ['BATCH'].split(',')[0], 'servers': "
            "[{'old_valid_until': '2026-11-01', 'info_valid_until': '2026-12-01', 'success': True}]}], 'hax'))\n"
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
        self.assertFalse(self.summary_file.exists())
        self.env.update(
            STATE_STATUS="changed",
            SAVE_OUTCOME="success",
            VERIFY_OUTCOME="success",
            VERIFY_HIT="true",
        )
        content = self.invoke()
        self.assertIn("已验证延长", content)
        self.assertIn("已发送 1 条", content)
        self.assertIn("已保存（已确认本轮缓存存在）", content)


if __name__ == "__main__":
    unittest.main()

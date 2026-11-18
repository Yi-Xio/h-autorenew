"""用示例配置验证准备工具更新、文件注入和本地正式入口衔接。"""

import copy
import contextlib
import io
import json
import os
import signal
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from scripts.accounts import public_account_id, runtime_account
from scripts.renewal_config import (
    configured_accounts,
    identifier_directory,
    pack_accounts,
)
from scripts.web_sessions import (
    AuthenticationError,
    load_private_json,
    save_private_json,
)
from tests.live import prepare, run
from tests.regression.fixtures import (
    PROJECT,
    web_account,
    web_environment,
    navigation_sample,
)


class PreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="hax-preparation-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patched = patch.object(prepare, "SECRETS", self.root / "secrets")
        patched.start()
        self.addCleanup(patched.stop)

    def test_preparation_installs_rules_on_hax_tab_before_navigation(
        self,
    ) -> None:
        events = []
        tabs = []

        def new_tab(url: str) -> object:
            events.append(("new_tab", url))
            page = SimpleNamespace(url=url)

            def navigate(target: str) -> None:
                events.append(("navigate", target))
                page.url = target

            page.get = navigate
            tabs.append(page)
            return page

        browser = SimpleNamespace(
            new_tab=new_tab,
            get_tabs=lambda: tabs,
            quit=lambda **kwargs: events.append("closed"),
        )
        with (
            patch.object(prepare, "LOCAL_ROOT", self.root),
            patch.object(prepare, "profile_address", return_value="127.0.0.1:9333"),
            patch.object(prepare, "Chromium", return_value=browser),
            patch.object(prepare.subprocess, "Popen") as process,
            patch.object(
                prepare,
                "install_hax_rules",
                side_effect=lambda page: events.append(("rules_registered", page)),
            ),
            patch.object(
                prepare,
                "export_from_page",
                side_effect=[web_account()["telegram_web"], web_account()["hax_web"]],
            ),
            patch("builtins.input", return_value=""),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            process.return_value.poll.return_value = None
            self.assertEqual(
                prepare.prepare_account("hax.co.id.us", web_account()["phone"])["id"],
                "hax.co.id.us",
            )
        arguments = process.call_args.args[0]
        self.assertEqual(arguments[-1], "about:blank")
        self.assertFalse(any(value.startswith("https://") for value in arguments))
        hax = next(tab for tab in tabs if tab.url == "https://hax.co.id/login")
        self.assertLess(
            events.index(("rules_registered", hax)),
            events.index(("navigate", "https://hax.co.id/login")),
        )
        self.assertLess(
            events.index(("rules_registered", hax)),
            events.index(("new_tab", "https://web.telegram.org/k/#@HaxTG_bot")),
        )
        process.return_value.terminate.assert_called_once()

    def test_failed_rules_registration_never_opens_hax_or_telegram(self) -> None:
        opened = []
        browser = SimpleNamespace(
            new_tab=lambda url: opened.append(url) or object(),
            quit=lambda **kwargs: None,
        )
        with (
            patch.object(prepare, "LOCAL_ROOT", self.root),
            patch.object(prepare, "profile_address", return_value="127.0.0.1:9333"),
            patch.object(prepare, "Chromium", return_value=browser),
            patch.object(prepare.subprocess, "Popen") as process,
            patch.object(
                prepare,
                "install_hax_rules",
                side_effect=AuthenticationError("hax_rules_unavailable"),
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            process.return_value.poll.return_value = None
            with self.assertRaises(AuthenticationError) as error:
                prepare.prepare_account("hax.co.id.us", web_account()["phone"])
        self.assertEqual(error.exception.reason, "hax_rules_unavailable")
        self.assertEqual(opened, ["about:blank"])
        process.return_value.terminate.assert_called_once()

    def test_interactive_entry_prepares_two_accounts_with_shared_notification(
        self,
    ) -> None:
        output = io.StringIO()
        with (
            patch.object(prepare.signal, "signal"),
            patch.object(prepare.sys.stdin, "isatty", return_value=True),
            patch(
                "builtins.input",
                side_effect=[
                    "A001",
                    "+1-5550001234",
                    "EXAMPLE_TOKEN",
                    "EXAMPLE_CHAT",
                    "y",
                    "A002",
                    "+1-5550005678",
                    "",
                    "n",
                ],
            ),
            patch.object(
                prepare, "prepare_account", side_effect=[web_account(), web_account(2)]
            ),
            contextlib.redirect_stdout(output),
        ):
            code = prepare.main([])
        self.assertEqual(code, 0)
        self.assertEqual(
            pack_accounts(prepare.SECRETS / "manifest.json"),
            dict(version=1, accounts=[web_account(), web_account(2)]),
        )
        self.assertEqual(
            load_private_json(prepare.SECRETS / "notification-token.json"),
            "EXAMPLE_TOKEN",
        )
        self.assertIn("accounts=2", output.getvalue())
        for value in (
            "EXAMPLE_TOKEN",
            "EXAMPLE_CHAT",
            "+1-5550001234",
            "+1-5550005678",
        ):
            self.assertNotIn(value, output.getvalue())

    def test_account_update_keeps_other_account_and_archives_original_bytes(
        self,
    ) -> None:
        # 配置边界使用动态 JSON，内容由正式校验器核对。
        original: dict[str, Any] = dict(
            version=1, accounts=[web_account(1), web_account(2)]
        )
        prepare.store_configuration(original, "EXAMPLE_TOKEN", "EXAMPLE_CHAT")
        previous = (prepare.SECRETS / "accounts.json").read_bytes()
        changed = copy.deepcopy(original)
        changed["accounts"][0]["hax_web"]["cookies"][0]["value"] = "EXAMPLE_UPDATED"
        prepare.store_configuration(changed, "EXAMPLE_TOKEN", "EXAMPLE_CHAT")
        self.assertEqual(pack_accounts(prepare.SECRETS / "manifest.json"), changed)
        archived = list((prepare.SECRETS / "archive").glob("*/accounts.json"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].read_bytes(), previous)
        self.assertEqual(
            load_private_json(
                prepare.SECRETS / identifier_directory("A002") / "hax.json"
            ),
            original["accounts"][1]["hax_web"],
        )

    def test_invalid_account_or_notification_preserves_existing_configuration(
        self,
    ) -> None:
        original = dict(version=1, accounts=[web_account()])
        prepare.store_configuration(original, "EXAMPLE_TOKEN", "EXAMPLE_CHAT")
        previous = (prepare.SECRETS / "accounts.json").read_bytes()
        for payload, token in (
            (dict(version=1, accounts=[web_account(), web_account()]), "EXAMPLE_TOKEN"),
            (original, "invalid token"),
        ):
            with self.assertRaises(ValueError):
                prepare.store_configuration(payload, token, "EXAMPLE_CHAT")
            self.assertEqual((prepare.SECRETS / "accounts.json").read_bytes(), previous)

    def test_update_rolls_back_if_replacement_fails_partway(self) -> None:
        original = dict(version=1, accounts=[web_account()])
        prepare.store_configuration(original, "EXAMPLE_TOKEN", "EXAMPLE_CHAT")
        previous = {
            p.relative_to(prepare.SECRETS): p.read_bytes()
            for p in prepare.SECRETS.rglob("*.json")
        }
        changed = dict(version=1, accounts=[web_account(), web_account(2)])
        replace = Path.replace

        def fail_new_token(path: Path, target: Path) -> Path:
            if "pending" in path.parts and path.name == "notification-token.json":
                raise OSError("example_interrupted_write")
            return replace(path, target)

        with patch.object(Path, "replace", fail_new_token), self.assertRaises(OSError):
            prepare.store_configuration(changed, "EXAMPLE_NEW_TOKEN", "EXAMPLE_CHAT")
        for path, content in previous.items():
            self.assertEqual((prepare.SECRETS / path).read_bytes(), content)
        self.assertEqual(pack_accounts(prepare.SECRETS / "manifest.json"), original)

    def test_interactive_entry_retries_empty_identifier_and_preserves_user_text(
        self,
    ) -> None:
        account = web_account()
        account["id"] = " hax.co.id.us / 美国 "
        output = io.StringIO()
        with (
            patch.object(prepare.signal, "signal"),
            patch.object(prepare.sys.stdin, "isatty", return_value=True),
            patch(
                "builtins.input",
                side_effect=[
                    "",
                    account["id"],
                    account["phone"],
                    "EXAMPLE_TOKEN",
                    "EXAMPLE_CHAT",
                    "n",
                ],
            ),
            patch.object(prepare, "prepare_account", return_value=account) as exporter,
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(prepare.main([]), 0)
        exporter.assert_called_once_with(account["id"], account["phone"])
        self.assertIn("账号标识符不能为空", output.getvalue())
        self.assertEqual(
            pack_accounts(prepare.SECRETS / "manifest.json"),
            dict(version=1, accounts=[account]),
        )

    def test_saved_accounts_use_safe_directories_and_keep_identifiers_in_manifest(
        self,
    ) -> None:
        first, second = web_account(), web_account(2)
        first["id"], second["id"] = "../../outside\x00", "香港 / 备用 " + "长" * 300
        accounts = dict(version=1, accounts=[first, second])
        prepare.store_configuration(accounts, "EXAMPLE_TOKEN", "EXAMPLE_CHAT")
        self.assertEqual(pack_accounts(prepare.SECRETS / "manifest.json"), accounts)
        for item in (first, second):
            path = prepare.SECRETS / identifier_directory(item["id"]) / "telegram.json"
            self.assertEqual(load_private_json(path), item["telegram_web"])
        self.assertFalse((self.root.parent / "outside").exists())


class LocalRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="hax-local-runtime-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.work = self.root / "work"
        self.work.mkdir(mode=0o700)
        shutil.copytree(
            PROJECT / "scripts",
            self.work / "scripts",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        self.source = self.root / "secrets"
        for name, value in (
            (
                "accounts.json",
                dict(version=1, accounts=[web_account(), web_account(2)]),
            ),
            ("notification-token.json", "EXAMPLE_TOKEN"),
            ("notification-chat.json", "EXAMPLE_CHAT"),
        ):
            save_private_json(self.source / name, value)
        self.private = self.root / "private"
        self.private.mkdir(mode=0o700)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        patched = patch.object(run, "ROOT", self.work)
        patched.start()
        self.addCleanup(patched.stop)
        old_umask = os.umask(0o077)
        self.addCleanup(os.umask, old_umask)

    def environment(self) -> dict[str, str]:
        return run.import_configuration(self.source, self.private, os.getuid())

    def test_local_configuration_does_not_inherit_unrelated_hax_values(self) -> None:
        with patch.dict(os.environ, HAX_UNRELATED="EXAMPLE_PRIVATE_VALUE", HAX_FORCE_RENEWAL="1"):
            env = self.environment()
        self.assertNotIn("HAX_UNRELATED", env)
        self.assertNotIn("HAX_FORCE_RENEWAL", env)

    def test_cli_runs_each_account_once_for_the_selected_command(self) -> None:
        for command, force in (("renew", False), ("renew", True), ("check-auth", False)):
            arguments = [command, "--force"] if force else [command]
            for codes in ((0, 0), (1, 0)):
                with (
                    self.subTest(command=command, force=force, codes=codes),
                    patch.object(run.signal, "signal"),
                    patch.dict(os.environ, HAX_DATA_UID=str(os.getuid())),
                    patch.object(run, "import_configuration", return_value=web_environment(2)),
                    patch.object(run, "run_account", side_effect=codes) as worker,
                ):
                    self.assertEqual(run.main(arguments), max(codes))
                    self.assertEqual(
                        [call.args[1:4] for call in worker.call_args_list],
                        [("A001", 1, command), ("A002", 2, command)],
                    )
                    self.assertEqual(
                        [call.kwargs["force"] for call in worker.call_args_list],
                        [force, force],
                    )

    def test_cli_requires_a_subcommand_and_force_belongs_to_renewal(self) -> None:
        with (
            patch.object(run, "import_configuration") as importer,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(run.main([]), 0)
            self.assertIn("check-auth", output.getvalue())
            self.assertIn("renew", output.getvalue())
            self.assertNotEqual(run.main(["check-auth", "--force"]), 0)
            self.assertNotEqual(run.main(["--mode", "readonly"]), 0)
        importer.assert_not_called()

    def test_killed_child_does_not_turn_overall_exit_status_into_success(self) -> None:
        with (
            patch.object(run.signal, "signal"),
            patch.dict(os.environ, HAX_DATA_UID=str(os.getuid())),
            patch.object(run, "import_configuration", return_value=web_environment()),
            patch.object(run, "run_account", return_value=-signal.SIGKILL) as worker,
        ):
            self.assertNotEqual(run.main(["check-auth"]), 0)
        worker.assert_called_once()

    def worker(
        self, skipped: bool = False, identifier: str = "A002", *, force: bool = False
    ) -> None:
        (self.work / "hax-renew.py").write_text(
            """import os, sys
from scripts.renewal_config import worker_account
from scripts.public_report import publish_results, record_notification
item = worker_account(os.environ)
assert item["id"] == """
            + repr(identifier)
            + """
assert "RENEWAL_ACCOUNTS" not in os.environ
assert "RENEWAL_ACCOUNTS_FILE" not in os.environ
assert os.environ.get("HAX_FORCE_RENEWAL") == """
            + repr("1" if force else None)
            + """
record_notification("text", True)
record_notification("photo", True)
sys.exit(publish_results([{"username": item["phone"], "servers": [{
    "success": True, "feedback_status": "success", "submission_attempted": True,
    "old_valid_until": "2026-11-21", "info_valid_until": "2026-11-21",
    "skipped": """
            + repr(skipped)
            + """}]}], "hax"))
"""
        )

    def test_local_files_use_actions_fields_and_isolate_the_formal_worker(self) -> None:
        self.worker()
        env = self.environment()
        self.assertIn("RENEWAL_ACCOUNTS", env)
        self.assertIn("TELEGRAM_BOT_TOKEN", env)
        self.assertNotIn("RENEWAL_ACCOUNTS_FILE", env)
        self.assertNotIn("TELEGRAM_BOT_TOKEN_FILE", env)
        destination = self.root / "renew"
        with patch.object(run, "connect_warp"):
            code = run.run_account(env, "A002", 2, "renew", destination, self.state)
        self.assertEqual(code, 0)
        receipt = load_private_json(destination / "receipt.json")
        if not isinstance(receipt, dict):
            raise AssertionError("invalid_receipt")
        self.assertEqual(receipt["result"], "renewed")
        self.assertEqual(receipt["input"], "actions")
        self.assertFalse(receipt["force"])
        self.assertFalse(receipt["github_cache_uploaded"])
        self.assertTrue(receipt["local_cache_saved"])
        self.assertIn("本地文件间模拟", (destination / "summary.md").read_text())
        self.assertFalse((self.work / "output").exists())
        self.assertEqual(len(configured_accounts(env)), 2)

    def test_only_force_bypasses_an_eligible_local_cache(self) -> None:
        env = self.environment()
        fields = runtime_account(
            "hax", dict(env, RENEWAL_ACCOUNT_ID="A002"), with_notification=False
        )
        key = public_account_id("hax", fields)
        cached = self.state / (key + ".json")
        expiry = "2099-12-31"
        save_private_json(cached, dict(version=1, platform="hax", account=key, state=expiry))
        original_workflow = run.workflow

        def workflow(operation: str, environment: dict[str, str]) -> tuple[int, dict[str, str]]:
            if operation == "notify-skipped":
                return 0, {"notification_status": "sent"}
            return original_workflow(operation, environment)

        normal, forced = self.root / "normal", self.root / "forced"
        self.worker(force=True)
        with (
            patch.object(run, "workflow", side_effect=workflow) as steps,
            patch.object(run, "connect_warp") as warp,
        ):
            self.assertEqual(run.run_account(env, "A002", 2, "renew", normal, self.state), 0)
            warp.assert_not_called()
            steps.reset_mock()
            self.assertEqual(
                run.run_account(env, "A002", 2, "renew", forced, self.state, force=True), 0
            )
            warp.assert_called_once()
            self.assertNotIn("check", [call.args[0] for call in steps.call_args_list])
        normal_receipt = load_private_json(normal / "receipt.json")
        forced_receipt = load_private_json(forced / "receipt.json")
        if not isinstance(normal_receipt, dict) or not isinstance(forced_receipt, dict):
            raise AssertionError("invalid_receipt")
        self.assertEqual(normal_receipt["result"], "skipped")
        self.assertEqual(forced_receipt["result"], "renewed")
        self.assertTrue(forced_receipt["force"])
        self.assertIn("强制实测，本地预筛已跳过", (forced / "summary.md").read_text())

    def test_local_receipt_separates_navigation_from_notification_counts(self) -> None:
        self.worker()
        worker = self.work / "hax-renew.py"
        sample = navigation_sample("loading")
        worker.write_text(
            worker.read_text().replace(
                "sys.exit(publish_results",
                "from scripts.public_report import record_navigation, record_check\n"
                + f"record_navigation({sample!r})\n"
                + "record_check('captcha', 'site_feedback', 'rejected')\n"
                + "sys.exit(publish_results",
            )
        )
        destination = self.root / "navigation-result"
        with patch.object(run, "connect_warp"):
            self.assertEqual(
                run.run_account(
                    self.environment(),
                    "A002",
                    2,
                    "renew",
                    destination,
                    self.state,
                ),
                0,
            )
        receipt = load_private_json(destination / "receipt.json")
        if not isinstance(receipt, dict):
            raise AssertionError("invalid_receipt")
        self.assertEqual(receipt["navigation"], [sample])
        self.assertNotIn("navigation", receipt["notifications"])
        self.assertEqual(
            receipt["checks"],
            {"captcha": {"step": "site_feedback", "result": "rejected"}},
        )
        self.assertNotIn("checks", receipt["notifications"])

    def test_arbitrary_identifier_reaches_formal_worker_without_entering_environment(
        self,
    ) -> None:
        first, second = web_account(), web_account(2)
        second["id"] = " hax.co.id.us / 美国\n::error::example\x00 "
        accounts_path = self.source / "accounts.json"
        accounts_path.unlink()
        save_private_json(accounts_path, dict(version=1, accounts=[first, second]))
        self.worker(identifier=second["id"])
        env = self.environment()
        with patch.object(run, "connect_warp"):
            self.assertEqual(
                run.run_account(env, second["id"], 2, "renew", self.root / "run", self.state), 0
            )

    def test_cli_selects_user_identifier_and_derives_safe_result_paths(self) -> None:
        first, second = web_account(), web_account(2)
        first["id"], second["id"] = "../美国 / 主机", "hax.co.id.us"
        env = web_environment(2)
        env["RENEWAL_ACCOUNTS"] = run.json.dumps(
            dict(version=1, accounts=[first, second])
        )
        with (
            patch.object(run.signal, "signal"),
            patch.dict(os.environ, HAX_DATA_UID=str(os.getuid())),
            patch.object(run, "import_configuration", return_value=env),
            patch.object(run, "run_account", return_value=1) as worker,
        ):
            self.assertEqual(
                run.main(["check-auth", "--account-identifier", first["id"]]), 1
            )
        worker.assert_called_once()
        self.assertEqual(worker.call_args.args[1], first["id"])
        destination = worker.call_args.args[4]
        self.assertEqual(destination.name, identifier_directory(first["id"]))
        self.assertEqual(destination.parent.parent, Path("/mnt/hax-results"))

    def test_site_skip_is_not_reported_as_full_renewal_success(self) -> None:
        self.worker(skipped=True, force=True)
        with patch.object(run, "connect_warp"):
            code = run.run_account(
                self.environment(),
                "A002",
                2,
                "renew",
                self.root / "skipped",
                self.state,
                force=True,
            )
        self.assertNotEqual(code, 0)
        receipt = load_private_json(self.root / "skipped/receipt.json")
        if not isinstance(receipt, dict):
            raise AssertionError("invalid_receipt")
        self.assertEqual(receipt["result"], "skipped")
        self.assertEqual(receipt["validation"], "incomplete")

    def test_damaged_local_cache_still_starts_formal_site_check(self) -> None:
        self.worker()
        env = self.environment()
        fields = runtime_account(
            "hax", dict(env, RENEWAL_ACCOUNT_ID="A002"), with_notification=False
        )
        cached = self.state / (public_account_id("hax", fields) + ".json")
        cached.write_text("invalid example cache")
        destination = self.root / "damaged-cache"
        with patch.object(run, "connect_warp"):
            code = run.run_account(
                env, "A002", 2, "renew", destination, self.state
            )
        self.assertEqual(code, 0)
        self.assertIn(
            "记录无效，改为检查网站", (destination / "summary.md").read_text()
        )
        snapshot = load_private_json(cached)
        if not isinstance(snapshot, dict):
            raise AssertionError("invalid_cache_snapshot")
        self.assertEqual(snapshot["state"], "2026-11-21")

    def test_readonly_authentication_requires_the_formal_warp_network(self) -> None:
        events = []
        env = self.environment()
        destination = self.root / "readonly-network"

        def authenticate(*args: object) -> int:
            self.assertEqual(events, ["warp_ready"])
            events.append("authentication")
            output = run.ROOT / run.AUTHENTICATION_FILE
            output.parent.mkdir(exist_ok=True)
            output.write_text(
                json.dumps(dict(version=1, status="valid", reason="none", checks={}))
            )
            return 0

        with (
            patch.object(
                run, "connect_warp", side_effect=lambda: events.append("warp_ready")
            ),
            patch.object(run, "invoke", side_effect=authenticate),
        ):
            self.assertEqual(
                run.run_account(
                    env,
                    "A002",
                    2,
                    "check-auth",
                    destination,
                    self.state,
                ),
                0,
            )
        self.assertEqual(events, ["warp_ready", "authentication"])
        self.assertEqual(list(self.state.iterdir()), [])
        with (
            patch.object(
                run, "connect_warp", side_effect=ValueError("warp_setup_failed")
            ),
            patch.object(run, "invoke") as worker,
            self.assertRaises(ValueError),
        ):
            run.run_account(
                env,
                "A002",
                2,
                "check-auth",
                self.root / "readonly-network-failed",
                self.state,
            )
        worker.assert_not_called()

    def test_readonly_receipt_keeps_formal_failure_reason_and_check(self) -> None:
        """本地回执沿用正式诊断，不把所有认证错误归为同一原因。"""
        destination = self.root / "readonly-diagnostic"
        authentication = dict(
            version=1,
            status="failed",
            reason="telegram_peer_unverified",
            checks={
                "authentication": {"step": "telegram_web", "result": "peer_loading"}
            },
        )

        def authenticate(*args: object) -> int:
            output = run.ROOT / run.AUTHENTICATION_FILE
            output.parent.mkdir(exist_ok=True)
            output.write_text(json.dumps(authentication))
            return 1

        with (
            patch.object(run, "connect_warp"),
            patch.object(run, "invoke", side_effect=authenticate),
        ):
            code = run.run_account(
                self.environment(),
                "A002",
                2,
                "check-auth",
                destination,
                self.state,
            )
        self.assertEqual(code, 1)
        receipt = load_private_json(destination / "receipt.json")
        if not isinstance(receipt, dict):
            raise AssertionError("invalid_receipt")
        self.assertEqual(receipt["reason"], authentication["reason"])
        self.assertEqual(receipt["checks"], authentication["checks"])
        self.assertEqual(list(self.state.iterdir()), [])

    def test_cancel_and_timeout_keep_explicit_receipt_without_saving_state(
        self,
    ) -> None:
        env = self.environment()
        for failure, result, reason in (
            (KeyboardInterrupt(), "cancelled", "local_validation_cancelled"),
            (
                run.subprocess.TimeoutExpired("example", 1),
                "failed",
                "local_validation_timeout",
            ),
        ):
            destination = self.root / result
            with (
                patch.object(run, "connect_warp"),
                patch.object(run, "invoke", side_effect=failure),
                self.assertRaises(type(failure)),
            ):
                run.run_account(
                    env, "A002", 2, "check-auth", destination, self.state
                )
            receipt = load_private_json(destination / "receipt.json")
            if not isinstance(receipt, dict):
                raise AssertionError("invalid_receipt")
            self.assertEqual(receipt["result"], result)
            self.assertEqual(receipt["reason"], reason)
            self.assertEqual(list(self.state.iterdir()), [])

    def test_missing_public_symlink_and_foreign_owned_credentials_stop_import(
        self,
    ) -> None:
        token = self.source / "notification-token.json"
        token.chmod(0o644)
        with self.assertRaises(ValueError):
            self.environment()
        token.chmod(0o600)
        token.rename(self.source / "saved.json")
        token.symlink_to(self.source / "saved.json")
        # 前一个失败已生成账号副本，另用空目录验证符号链接拒绝。
        other = self.root / "other"
        other.mkdir(mode=0o700)
        with self.assertRaises(OSError):
            run.import_configuration(self.source, other, os.getuid())
        empty = self.root / "empty"
        empty.mkdir(mode=0o700)
        with self.assertRaises(ValueError):
            run.import_configuration(self.source, empty, os.getuid() + 1)

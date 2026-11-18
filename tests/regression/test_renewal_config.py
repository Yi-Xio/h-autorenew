"""从配置、打包工具和正式工作流入口验证账号边界。"""

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from scripts.renewal_config import (
    cache_identity,
    configured_accounts,
    identifier_directory,
    notification_config,
    parse_accounts,
    select_account,
    worker_account,
)
from scripts.web_sessions import save_private_json, save_snapshot
from tests.regression.fixtures import web_account, web_environment

from tests.regression.fixtures import PROJECT


class ConfigurationTests(unittest.TestCase):
    """配置错误不得变成其他账号或旧认证的隐式回退。"""

    def test_selects_stable_id_after_reordering(self) -> None:
        environment = web_environment(2)
        environment["RENEWAL_ACCOUNT_ID"] = "A002"
        index, account = select_account(environment)
        self.assertEqual((index, account["id"]), (2, "A002"))
        environment["RENEWAL_ACCOUNTS"] = json.dumps(
            dict(version=1, accounts=[web_account(2), web_account(1)])
        )
        index, account = select_account(environment)
        self.assertEqual((index, account["id"]), (1, "A002"))
        environment["RENEWAL_ACCOUNT_ID"] = "A999"
        with self.assertRaises(ValueError):
            select_account(environment)

    def test_rejects_malformed_duplicate_mismatched_and_oversized_input(self) -> None:
        # 示例涵盖不同 JSON 类型，由配置校验器判断是否合法。
        valid: dict[str, Any] = dict(version=1, accounts=[web_account()])
        invalid = [
            None,
            {},
            dict(version=True, accounts=[web_account()]),
            dict(version=1, accounts=[]),
        ]
        for field, value in (
            ("id", ""),
            ("id", None),
            ("phone", "+1-5550000999"),
            ("extra", "PRIVATE"),
        ):
            item = copy.deepcopy(valid)
            item["accounts"][0][field] = value
            invalid.append(item)
        mismatched = copy.deepcopy(valid)
        mismatched["accounts"][0]["hax_web"] = web_account(2)["hax_web"]
        invalid.append(mismatched)
        invalid.append(dict(version=1, accounts=[web_account(), web_account()]))
        for item in invalid:
            with (
                self.subTest(item_type=type(item).__name__),
                self.assertRaises(ValueError),
            ):
                parse_accounts(json.dumps(item))
        for raw in ('{"version":1,"version":1,"accounts":[]}', "x" * 49153):
            with self.assertRaises(ValueError):
                parse_accounts(raw)

    def test_arbitrary_identifiers_select_exact_accounts_without_normalization(
        self,
    ) -> None:
        identifiers = (
            "hax.co.id.us",
            "香港 主机 / 备用 🚀",
            " ../outside\\name \n::error::example",
            "\x00",
            " ",
            "x" * 1000,
        )
        for identifier in identifiers:
            with self.subTest(identifier=repr(identifier)):
                first, second = web_account(), web_account(2)
                first["id"] = identifier
                env = dict(
                    RENEWAL_ACCOUNTS=json.dumps(
                        dict(version=1, accounts=[second, first])
                    ),
                    RENEWAL_ACCOUNT_ID=identifier,
                )
                self.assertEqual(select_account(env), (2, first))
                del env["RENEWAL_ACCOUNT_ID"]
                env["RENEWAL_ACCOUNT_KEY"] = cache_identity(first)
                self.assertEqual(select_account(env), (2, first))

    def test_duplicate_identifiers_and_conflicting_selectors_are_rejected(self) -> None:
        first, second = web_account(), web_account(2)
        first["id"] = second["id"] = "主机 / us"
        with self.assertRaises(ValueError):
            parse_accounts(json.dumps(dict(version=1, accounts=[first, second])))
        env = web_environment()
        env["RENEWAL_ACCOUNT_KEY"] = cache_identity(web_account())
        with self.assertRaises(ValueError):
            select_account(env)
        del env["RENEWAL_ACCOUNT_ID"]
        for key in ("", "wrong", "0" * 64):
            env["RENEWAL_ACCOUNT_KEY"] = key
            with self.assertRaises(ValueError):
                select_account(env)

    def test_directory_names_are_bounded_and_distinguish_case_and_unicode(self) -> None:
        identifiers = (
            "A",
            "a",
            "é",
            "e\u0301",
            "../outside",
            "/tmp/outside",
            "x" * 1000,
        )
        names = [identifier_directory(value) for value in identifiers]
        self.assertEqual(len(set(names)), len(identifiers))
        for name in names:
            self.assertRegex(name, r"^account-[0-9a-f]{64}$")

    def test_cache_ignores_serialization_metadata_and_hax_cookie_refresh(self) -> None:
        original = web_account()
        changed = copy.deepcopy(original)
        changed["telegram_web"]["account"]["date"] = 1700000000
        changed["hax_web"]["cookies"][0]["value"] = "CHANGED" + "x" * 24
        self.assertEqual(cache_identity(original), cache_identity(changed))
        changed["telegram_web"]["account"]["dc2_auth_key"] = "cd" * 256
        self.assertNotEqual(cache_identity(original), cache_identity(changed))
        self.assertNotEqual(cache_identity(original), cache_identity(web_account(2)))

    def test_private_file_input_and_worker_reject_full_collection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            os.chmod(root, 0o700)
            path = root / "accounts.json"
            save_private_json(path, dict(version=1, accounts=[web_account()]))
            environment = dict(RENEWAL_ACCOUNTS_FILE=str(path))
            self.assertEqual(len(configured_accounts(environment)), 1)
            environment["RENEWAL_ACCOUNTS"] = "PRIVATE"
            with self.assertRaises(ValueError):
                configured_accounts(environment)
            worker = dict(
                HAX_ACCOUNT_FILE=str(path), PUBLIC_REPORT_FILE=str(root / "report.json")
            )
            self.assertEqual(worker_account(worker)["id"], "A001")
            worker["RENEWAL_ACCOUNTS"] = "PRIVATE"
            with self.assertRaises(ValueError):
                worker_account(worker)

    def test_notification_file_and_environment_sources_are_unambiguous(self) -> None:
        environment = web_environment()
        self.assertEqual(
            notification_config(environment),
            (environment["TELEGRAM_BOT_TOKEN"], environment["TELEGRAM_CHAT_ID"]),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            os.chmod(root, 0o700)
            for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
                path = root / (name + ".json")
                save_private_json(path, environment.pop(name))
                environment[name + "_FILE"] = str(path)
            self.assertEqual(notification_config(environment)[1], "chat-9988")
            environment["TELEGRAM_CHAT_ID"] = "conflicting"
            with self.assertRaises(ValueError):
                notification_config(environment)

    def test_pack_cli_embeds_objects_preserves_accounts_and_checks_privacy(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            os.chmod(root, 0o700)
            manifest: dict[str, Any] = dict(version=1, accounts=[])
            for number in (1, 2):
                account = web_account(number)
                for kind in ("telegram", "hax"):
                    save_snapshot(
                        root / f"{kind}-{number}.json", account[f"{kind}_web"]
                    )
                manifest["accounts"].append(
                    dict(
                        id=account["id"],
                        phone=account["phone"],
                        telegram_web_file=f"telegram-{number}.json",
                        hax_web_file=f"hax-{number}.json",
                    )
                )
            save_private_json(root / "manifest.json", manifest)
            output = root / "packed.json"
            command = [
                sys.executable,
                str(PROJECT / "setup_renewal_accounts.py"),
                "pack",
                "--input",
                str(root / "manifest.json"),
                "--output",
                str(output),
            ]
            result = subprocess.run(command, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(
                len(configured_accounts(dict(RENEWAL_ACCOUNTS_FILE=str(output)))), 2
            )
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(web_account()["phone"], result.stdout + result.stderr)
            again = subprocess.run(command, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(again.returncode, 0)
            self.assertNotIn("Traceback", again.stdout + again.stderr)

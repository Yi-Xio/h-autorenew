"""用示例会话验证最小导出、账号配对、私有文件和命令行输出。"""

import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import setup_web_sessions
from scripts.renewal_config import identifier_directory
from tests.regression.fixtures import WidgetPage, login_cookies
from scripts.web_sessions import (
    HAX_CLIENT,
    MAX_SESSION_BYTES,
    SessionError,
    hax_snapshot,
    load_snapshot,
    parse_snapshot,
    save_snapshot,
    telegram_snapshot,
    validate_snapshot,
)

PHONE = "+1-5550001234"
SECOND_PHONE = "+1-5550005678"
USER_ID = "900000001"
KEY = "ab" * 256
COOKIE = "ef" * 16


def telegram() -> dict:
    """生成不含真实凭据的已登录 Web K 槽位。"""
    return {
        "phone": "15550001234",
        "userId": USER_ID,
        "dcId": 2,
        "dc2_auth_key": KEY,
        "dc2_server_salt": "cd" * 8,
        "auth_key_fingerprint": "ab" * 4,
        "date": 1700000000,
    }


def cookie() -> dict:
    """保留实际浏览器允许省略 SameSite 的形式。"""
    return {
        "name": "stel_ssid",
        "value": COOKIE,
        "domain": "oauth.telegram.org",
        "path": "/",
        "secure": True,
        "httpOnly": True,
        "expires": -1,
    }


class FakePage:
    """只提供导出需要的只读浏览器接口。"""

    def __init__(self, kind: str, ready: bool = True) -> None:
        self.kind = kind
        self.ready = ready
        self.browser = SimpleNamespace(
            new_tab=lambda url=None: WidgetPage(
                payload=None
                if ready
                else {"origin": "https://hax.co.id", "user": False}
            )
        )
        self.url = (
            "https://web.telegram.org/k/#@HaxTG_bot"
            if kind == "telegram"
            else "https://hax.co.id/vps-info/"
        )

    def run_js(self, script: str) -> object:
        if self.kind == "telegram":
            return {"account1": json.dumps(telegram())} if self.ready else None
        return self.ready

    def run_cdp(self, method: str, **kwargs: object) -> dict:
        return {
            "cookies": [
                cookie(),
                login_cookies()[1],
                {"name": "cf_clearance", "value": "EXAMPLE_CLEARANCE"},
            ]
        }


class WebSessionTests(unittest.TestCase):
    """从导出、文件及 CLI 的公开接口检查安全边界。"""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="hax-test-web-session-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.telegram = telegram_snapshot({"account1": json.dumps(telegram())}, PHONE)
        self.hax = hax_snapshot(
            [cookie(), login_cookies()[1]], self.telegram, confirmed_account=True
        )

    def test_export_only_keeps_one_accounts_permanent_authorization(self) -> None:
        full = {
            **telegram(),
            "firstName": "FAKE_NAME",
            "lastName": "FAKE_LAST",
            "isPremium": False,
            "push_key": "FAKE_PUSH",
            "temp_auth_keys": {"2": "FAKE_TEMP"},
        }
        storage = {
            "account1": json.dumps(full),
            "account2": json.dumps({"dc2_auth_key": "FAKE_UNUSED"}),
            "chats": "FAKE_HISTORY",
            "other_origin": "FAKE_TOKEN",
        }
        exported = telegram_snapshot(storage, PHONE)
        self.assertEqual(exported, self.telegram)
        self.assertEqual(set(exported), {"version", "client", "origin", "account"})
        for value in (
            "FAKE_NAME",
            "FAKE_LAST",
            "FAKE_PUSH",
            "FAKE_TEMP",
            "FAKE_UNUSED",
            "FAKE_HISTORY",
            "FAKE_TOKEN",
        ):
            self.assertNotIn(value, json.dumps(exported))

    def test_wrong_missing_multiple_and_unknown_accounts_fail(self) -> None:
        cases = [
            ({}, PHONE, "not_logged_in"),
            ({"account1": json.dumps(telegram())}, SECOND_PHONE, "account_mismatch"),
            (
                {
                    "account1": json.dumps(telegram()),
                    "account2": json.dumps(telegram()),
                },
                PHONE,
                "multiple_accounts",
            ),
            (
                {
                    "account1": json.dumps(
                        {**telegram(), "new_unknown_field": "FAKE_TOKEN"}
                    )
                },
                PHONE,
                "invalid_session",
            ),
            ({"account1": "FAKE_SESSION"}, PHONE, "invalid_session"),
        ]
        for storage, phone, reason in cases:
            with self.subTest(reason=reason), self.assertRaises(SessionError) as error:
                telegram_snapshot(storage, phone)
            self.assertEqual(str(error.exception), reason)

    def test_hax_export_requires_confirmation_and_excludes_clearance_and_other_sites(
        self,
    ) -> None:
        with self.assertRaises(SessionError) as error:
            hax_snapshot([cookie()], self.telegram)
        self.assertEqual(error.exception.code, "confirmation_required")
        captured = hax_snapshot(
            [
                cookie(),
                login_cookies()[1],
                {**cookie(), "path": "/home/", "value": "FAKE_OTHER_PATH"},
                {
                    "name": "cf_clearance",
                    "value": "FAKE_CLEARANCE",
                    "domain": ".hax.co.id",
                },
                {
                    **cookie(),
                    "domain": "hax.co.id.evil.invalid",
                    "value": "FAKE_OTHER_SITE",
                },
            ],
            self.telegram,
            confirmed_account=True,
        )
        self.assertEqual(captured, self.hax)
        self.assertEqual(captured["binding"], "telegram-widget")
        for value in ("FAKE_CLEARANCE", "FAKE_OTHER_PATH", "FAKE_OTHER_SITE"):
            self.assertNotIn(value, json.dumps(captured))

    def test_malformed_schema_cannot_be_used_as_a_session(self) -> None:
        values = [
            [],
            {**self.telegram, "version": True},
            {**self.telegram, "client": []},
            {**self.telegram, "origin": "https://web.telegram.org.evil.invalid"},
            {**self.telegram, "extra": "FAKE_TOKEN"},
            {**self.telegram, "account": {**telegram(), "dcId": True}},
            {**self.telegram, "account": {**telegram(), "dcId": 6}},
            {**self.telegram, "account": {**telegram(), "dc2_auth_key": "FAKE_KEY"}},
            {**self.telegram, "account": {**telegram(), "dc2_server_salt": False}},
            {**self.telegram, "account": {**telegram(), "userId": 900000001}},
            {**self.telegram, "account": {**telegram(), "date": True}},
            {**self.hax, "binding": "server-verified"},
            {**self.hax, "cookies": [cookie(), cookie()]},
        ]
        for value in values:
            with (
                self.subTest(value_type=type(value).__name__),
                self.assertRaises(SessionError),
            ):
                validate_snapshot(value)
        for change in (
            {"domain": ".hax.co.id"},
            {"path": "/login"},
            {"sameSite": []},
            {"secure": 1},
            {"expires": True},
            {"expires": float("nan")},
            {"value": "FAKE_TOKEN\r\nheader"},
            {"url": "https://evil.invalid"},
        ):
            with self.subTest(change=list(change)), self.assertRaises(SessionError):
                validate_snapshot(
                    {
                        **self.hax,
                        "cookies": [{**cookie(), **change}, login_cookies()[1]],
                    }
                )

    def test_duplicate_and_oversized_json_are_rejected_without_input_echo(self) -> None:
        for raw in (
            '{"version":1,"version":1,"secret":"FAKE_TOKEN"}',
            json.dumps(self.telegram).replace('"dcId": 2', '"dcId": 2, "dcId": 2'),
            "FAKE_TOKEN" * MAX_SESSION_BYTES,
            "\ud800",
        ):
            with (
                self.subTest(length=len(raw)),
                self.assertRaises(SessionError) as error,
            ):
                parse_snapshot(raw)
            self.assertNotIn("FAKE_TOKEN", str(error.exception))

    def test_phone_user_and_client_binding_are_independent_checks(self) -> None:
        for expected in (
            {"expected_phone": "15550005678"},
            {"expected_user_id": "900000002"},
            {"expected_client": HAX_CLIENT},
        ):
            with (
                self.subTest(expected=list(expected)),
                self.assertRaises(SessionError) as error,
            ):
                validate_snapshot(self.telegram, **expected)
            self.assertEqual(error.exception.code, "account_mismatch")

    def test_private_files_round_trip_without_overwriting_existing_credentials(
        self,
    ) -> None:
        path = self.root / "private" / "session.json"
        save_snapshot(path, self.telegram)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(load_snapshot(path), self.telegram)
        original = path.read_bytes()
        with self.assertRaises(SessionError) as error:
            save_snapshot(path, self.hax)
        self.assertEqual(error.exception.code, "file_exists")
        self.assertEqual(path.read_bytes(), original)

    def test_public_directories_git_aliases_and_symlinks_cannot_receive_credentials(
        self,
    ) -> None:
        public = self.root / "public"
        public.mkdir(mode=0o755)
        public.chmod(0o755)
        repository = self.root / "repository"
        repository.mkdir()
        (repository / ".git").mkdir()
        alias = self.root / "alias"
        alias.symlink_to(repository, target_is_directory=True)
        for path in (
            public / "session.json",
            repository / "session.json",
            alias / "session.json",
        ):
            with self.subTest(path=path.name), self.assertRaises(SessionError):
                save_snapshot(path, self.telegram)
            self.assertFalse(path.exists())
        private = self.root / "private.json"
        save_snapshot(private, self.telegram)
        link = self.root / "linked.json"
        link.symlink_to(private)
        with self.assertRaises(SessionError):
            save_snapshot(link, self.hax)
        with self.assertRaises(SessionError):
            load_snapshot(link)
        private.chmod(0o644)
        with self.assertRaises(SessionError):
            load_snapshot(private)

    def test_wrong_origin_login_and_unverified_pages_cannot_export(self) -> None:
        for kind, urls in (
            (
                "telegram",
                [
                    "https://web.telegram.org/a/",
                    "https://web.telegram.org.evil.invalid/k/",
                    "http://web.telegram.org/k/",
                ],
            ),
            (
                "hax",
                [
                    "https://hax.co.id/home/",
                    "https://hax.co.id@evil.invalid/vps-info/",
                    "https://hax.co.id:bad/vps-info/",
                    "https://hax.co.id:8443/vps-info/",
                ],
            ),
        ):
            for url in urls:
                page = FakePage(kind)
                page.url = url
                with self.subTest(kind=kind), self.assertRaises(SessionError):
                    setup_web_sessions.export_from_page(
                        page, kind, PHONE, self.telegram, True
                    )
            with self.subTest(ready=False, kind=kind), self.assertRaises(SessionError):
                setup_web_sessions.export_from_page(
                    FakePage(kind, ready=False), kind, PHONE, self.telegram, True
                )

    def test_cli_exports_with_driver_streams_suppressed_and_checks_pairing_offline(
        self,
    ) -> None:
        profile = self.root / "profile"
        profile.mkdir()
        (profile / "DevToolsActivePort").write_text(
            "12345\n/devtools/browser/example-owned\n"
        )
        tg_path, hax_path = self.root / "telegram.json", self.root / "hax.json"

        def connect(address: str) -> object:
            print("FAKE_DRIVER_SECRET")
            return type(
                "Browser",
                (),
                {"get_tabs": lambda _: [FakePage("telegram"), FakePage("hax")]},
            )()

        output = io.StringIO()
        with (
            patch.object(setup_web_sessions, "Chromium", connect),
            patch.object(
                setup_web_sessions,
                "build_opener",
                side_effect=lambda *args: SimpleNamespace(
                    open=lambda *args, **kwargs: io.BytesIO(
                        json.dumps(
                            {
                                "webSocketDebuggerUrl": "ws://127.0.0.1:12345/devtools/browser/example-owned"
                            }
                        ).encode()
                    )
                ),
            ),
            patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}),
            contextlib.redirect_stdout(output),
        ):
            for kind, target in (("telegram", tg_path), ("hax", hax_path)):
                args = [
                    "export",
                    "--kind",
                    kind,
                    "--phone",
                    PHONE,
                    "--profile",
                    str(profile),
                    "--output",
                    str(target),
                ]
                if kind == "hax":
                    args += ["--telegram", str(tg_path), "--confirm-hax-account"]
                self.assertEqual(setup_web_sessions.main(args), 0)
            self.assertEqual(
                setup_web_sessions.main(
                    [
                        "check",
                        "--kind",
                        "hax",
                        "--phone",
                        PHONE,
                        "--input",
                        str(hax_path),
                        "--telegram",
                        str(tg_path),
                    ]
                ),
                0,
            )
            wrong = copy.deepcopy(self.telegram)
            wrong["account"]["userId"] = "900000002"
            wrong_path = self.root / "wrong.json"
            save_snapshot(wrong_path, wrong)
            self.assertEqual(
                setup_web_sessions.main(
                    [
                        "check",
                        "--kind",
                        "hax",
                        "--phone",
                        PHONE,
                        "--input",
                        str(hax_path),
                        "--telegram",
                        str(wrong_path),
                    ]
                ),
                1,
            )
        self.assertIn("online=not_checked", output.getvalue())
        for secret in (KEY, COOKIE, USER_ID, "FAKE_DRIVER_SECRET", "15550001234"):
            self.assertNotIn(secret, output.getvalue())

    def test_export_cli_uses_safe_default_paths_for_arbitrary_identifier(self) -> None:
        identifier = " ../美国 / 主机 " + "长" * 300
        directory = identifier_directory(identifier)
        browser = SimpleNamespace(get_tabs=lambda: [FakePage("telegram")])
        output = io.StringIO()
        with (
            patch.object(setup_web_sessions, "LOCAL_ROOT", self.root),
            patch.object(
                setup_web_sessions, "profile_address", return_value="127.0.0.1:12345"
            ) as address,
            patch.object(setup_web_sessions, "Chromium", return_value=browser),
            patch.dict(os.environ, GITHUB_ACTIONS="false"),
            contextlib.redirect_stdout(output),
        ):
            code = setup_web_sessions.main(
                [
                    "export",
                    "--kind",
                    "telegram",
                    "--phone",
                    PHONE,
                    "--account-identifier",
                    identifier,
                ]
            )
        self.assertEqual(code, 0, output.getvalue())
        address.assert_called_once_with(self.root / "browser" / directory)
        self.assertEqual(
            load_snapshot(self.root / "secrets" / directory / "telegram.json"),
            self.telegram,
        )
        self.assertNotIn(identifier, output.getvalue())

    def test_reused_port_or_foreign_browser_marker_is_rejected_before_connection(
        self,
    ) -> None:
        profile = self.root / "profile"
        profile.mkdir()
        (profile / "DevToolsActivePort").write_text(
            "12345\n/devtools/browser/example-owned\n"
        )
        for endpoint in (
            "ws://127.0.0.1:12345/devtools/browser/another-instance",
            "ws://unrelated.invalid:12345/devtools/browser/example-owned",
        ):
            output = io.StringIO()
            with (
                patch.object(
                    setup_web_sessions,
                    "build_opener",
                    return_value=SimpleNamespace(
                        open=lambda *args, **kwargs: io.BytesIO(
                            json.dumps({"webSocketDebuggerUrl": endpoint}).encode()
                        )
                    ),
                ),
                patch.object(setup_web_sessions, "Chromium") as connect,
                patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}),
                contextlib.redirect_stdout(output),
            ):
                result = setup_web_sessions.main(
                    [
                        "export",
                        "--kind",
                        "telegram",
                        "--phone",
                        PHONE,
                        "--profile",
                        str(profile),
                        "--output",
                        str(self.root / "export.json"),
                    ]
                )
            self.assertEqual(result, 1)
            connect.assert_not_called()
            self.assertNotIn(endpoint, output.getvalue())

    def test_cli_refuses_actions_export_and_does_not_echo_invalid_arguments(
        self,
    ) -> None:
        output = io.StringIO()
        with (
            patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(
                setup_web_sessions.main(
                    ["export", "--kind", "telegram", "--phone", PHONE]
                ),
                1,
            )
            self.assertEqual(setup_web_sessions.main(["FAKE_TOKEN"]), 1)
        self.assertIn("reason=local_only", output.getvalue())
        self.assertNotIn("FAKE_TOKEN", output.getvalue())


if __name__ == "__main__":
    unittest.main()

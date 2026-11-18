"""隔离浏览器依赖，验证两份源码实际使用的发送函数。"""

import ast
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

from scripts.public_report import (
    publish_progress,
    record_notification,
    validate_snapshot,
)
from scripts.telegram import telegram_accepted
from tests.regression.fixtures import PROJECT


class TelegramTests(unittest.TestCase):
    """HTTP、API 返回和网络失败均使用替身，不发送真实通知。"""

    def test_http_api_and_network_outcomes_are_recorded_once_after_retries(
        self,
    ) -> None:
        for platform in ("hax", "woiden"):
            with (
                self.subTest(platform=platform),
                tempfile.TemporaryDirectory(prefix="hax-test-tg-") as directory,
            ):
                root = Path(directory)
                report = root / "report.json"
                photo = root / "account-vps_info.png"
                photo.write_bytes(b"test")
                post = Mock()
                namespace: dict[str, Any] = {
                    "os": os,
                    "time": SimpleNamespace(sleep=lambda _: None),
                    "requests": SimpleNamespace(post=post),
                    "telegram_accepted": telegram_accepted,
                    "record_notification": record_notification,
                    "publish_progress": publish_progress,
                }
                tree = ast.parse((PROJECT / f"{platform}-renew.py").read_text())
                functions: list[ast.stmt] = [
                    node
                    for node in tree.body
                    if isinstance(node, ast.FunctionDef)
                    and node.name in {"send_tg_message", "send_tg_photo"}
                ]
                self.assertEqual(len(functions), 2)
                exec(
                    compile(
                        ast.Module(body=functions, type_ignores=[]),
                        "<telegram functions>",
                        "exec",
                    ),
                    namespace,
                )
                for code, body, accepted in (
                    (200, {"ok": True}, True),
                    (200, {"ok": False, "description": "FAKE_SESSION"}, False),
                    (500, {"ok": True}, False),
                ):
                    for kind in ("text", "photo"):
                        with (
                            self.subTest(code=code, kind=kind),
                            patch.dict(os.environ, {"PUBLIC_REPORT_FILE": str(report)}),
                        ):
                            publish_progress("preparing")
                            post.reset_mock()
                            post.side_effect = None
                            post.return_value = SimpleNamespace(
                                status_code=code, json=lambda: body
                            )
                            output = io.StringIO()
                            with contextlib.redirect_stdout(output):
                                result = namespace[
                                    f"send_tg_{'message' if kind == 'text' else 'photo'}"
                                ](
                                    "FAKE_TOKEN",
                                    "FAKE_CHAT",
                                    "message" if kind == "text" else str(photo),
                                )
                            self.assertEqual(result, accepted)
                            self.assertEqual(post.call_count, 1)
                            value = json.loads(report.read_text())
                            validate_snapshot(value)
                            self.assertEqual(
                                value["details"][
                                    f"{kind}_{'sent' if accepted else 'failed'}"
                                ],
                                1,
                            )
                            self.assertNotIn(
                                "FAKE_", output.getvalue() + report.read_text()
                            )
                with patch.dict(os.environ, {"PUBLIC_REPORT_FILE": str(report)}):
                    publish_progress("preparing")
                    post.reset_mock()
                    post.side_effect = OSError("FAKE_SESSION")
                    self.assertFalse(
                        namespace["send_tg_message"](
                            "FAKE_TOKEN", "FAKE_CHAT", "message"
                        )
                    )
                    self.assertEqual(post.call_count, 3)
                    self.assertEqual(
                        json.loads(report.read_text())["details"]["text_failed"], 1
                    )
                    post.reset_mock()
                    self.assertFalse(
                        namespace["send_tg_photo"](
                            "FAKE_TOKEN", "FAKE_CHAT", str(root / "missing.png")
                        )
                    )
                    self.assertEqual(post.call_count, 0)
                    self.assertEqual(
                        json.loads(report.read_text())["details"]["photo_failed"], 0
                    )


if __name__ == "__main__":
    unittest.main()

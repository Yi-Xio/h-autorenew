"""隔离浏览器依赖，调用两份源码的公开截图和通知函数。"""

import ast
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

from scripts.public_report import publish_progress, renewal_verified, screenshot_allowed
from tests.regression.fixtures import PROJECT


class FakeBrowser:
    """只提供公开截图接口，不启动真实浏览器。"""

    def __init__(self, url: str, html: str) -> None:
        self.url = url
        self.html = html
        self.captured = []

    def get_current_url(self) -> str:
        return self.url

    def get_page_source(self) -> str:
        return self.html

    def save_screenshot(self, path: str) -> None:
        self.captured.append(path)
        Path(path).write_bytes(b"test image")


class ScreenshotTests(unittest.TestCase):
    """在不导入浏览器及 OCR 库的情况下验证实际函数。"""

    def test_only_confirmed_vps_page_can_be_captured_and_notified(self) -> None:
        for platform, domain in (("hax", "hax.co.id"), ("woiden", "woiden.id")):
            with (
                self.subTest(platform=platform),
                tempfile.TemporaryDirectory(prefix="hax-test-images-") as temporary,
            ):
                private = Path(temporary)
                photos = []
                texts = []
                # 动态执行原文件中的公开函数，避免加载实际浏览器依赖。
                namespace: Dict[str, Any] = {
                    "Path": Path,
                    "re": __import__("re"),
                    "screenshot_allowed": screenshot_allowed,
                    "renewal_verified": renewal_verified,
                    "publish_progress": publish_progress,
                    f"{platform.upper()}_BASE_URL": f"https://{domain}",
                    f"{platform.upper()}_TITLE": domain,
                    "mask": lambda value: "***",
                    "_tg_header": lambda *args, **kwargs: "",
                    "send_tg_message": lambda token, chat, text: texts.append(text),
                    "send_tg_photo": lambda token, chat, path, caption: photos.append(
                        path
                    ),
                    "requests": SimpleNamespace(
                        post=lambda url, **kwargs: texts.append(kwargs["json"]["text"])
                    ),
                }
                tree = ast.parse((PROJECT / f"{platform}-renew.py").read_text())
                functions: list[ast.stmt] = [
                    node
                    for node in tree.body
                    if isinstance(node, ast.FunctionDef)
                    and node.name in {"safe_screenshot", "notify"}
                ]
                self.assertEqual(len(functions), 2)
                exec(
                    compile(
                        ast.Module(body=functions, type_ignores=[]),
                        str(PROJECT / f"{platform}-renew.py"),
                        "exec",
                    ),
                    namespace,
                )
                destination = str(private / "acc1-000000-vps_info.png")
                marker = "<label>Hostname</label><label>Status</label><label>Valid until</label>"
                browser = FakeBrowser(f"https://{domain}/login", marker)
                with contextlib.redirect_stdout(io.StringIO()):
                    namespace["safe_screenshot"](browser, destination)
                    browser.url = f"https://{domain}/vps-info"
                    namespace["safe_screenshot"](
                        browser, str(private / "acc1-000000-login_phone_filled.png")
                    )
                    browser.url = "https://other.invalid/vps-info"
                    namespace["safe_screenshot"](browser, destination)
                    browser.url = f"https://{domain}/vps-info"
                    browser.html = "<form>Telegram verification code</form>"
                    namespace["safe_screenshot"](browser, destination)
                    self.assertEqual(browser.captured, [])
                    browser.html = marker
                    namespace["safe_screenshot"](browser, destination)
                    self.assertEqual(browser.captured, [destination])
                    namespace["notify"](
                        {
                            "screenshot": str(private / "disabled-result.png"),
                            "vps_info_screenshot": destination,
                            "success": True,
                            "old_valid_until": "2026-11-10",
                            "new_valid_until": "2026-11-20",
                        },
                        "+1-5550001234",
                        tg_token="FAKE_TOKEN",
                        tg_chat="FAKE_CHAT",
                    )
                self.assertEqual(photos, [destination])
                self.assertIn("续期结果待核实", texts[0])
                self.assertNotIn("✅ 续期成功", texts[0])


if __name__ == "__main__":
    unittest.main()

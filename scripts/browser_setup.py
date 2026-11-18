"""在首次导航前注册 Hax 页面规则，并识别已知广告检测弹窗。"""

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary

from scripts.web_sessions import AuthenticationError

ADBLOCK_MESSAGE = "Please disable AdBlock to access this site!"
RULES_FILE = Path(__file__).with_name("hax_rules.js")

# DrissionPage 页面和 CDP 返回动态对象；这里只记录当前页面的注册标识。
_registrations: WeakKeyDictionary[Any, str] = WeakKeyDictionary()


def install_hax_rules(page: Any) -> None:
    """为当前标签页注册文档前脚本；新标签页和新浏览器需要分别调用。"""
    try:
        if page in _registrations:
            return
        source = RULES_FILE.read_text(encoding="utf-8")
        if not source.strip():
            raise ValueError("empty_rules")
        result = page.run_cdp(
            "Page.addScriptToEvaluateOnNewDocument", source=source, _timeout=5
        )
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("identifier"), str)
            or not result["identifier"]
        ):
            raise ValueError("registration_failed")
        _registrations[page] = result["identifier"]
    except Exception:
        raise AuthenticationError("hax_rules_unavailable") from None


def has_adblock_dialog(page: Any) -> bool:
    """只读取 Hax 的已知拦截弹窗，不确认或取消其他弹窗。"""
    try:
        handler = getattr(page, "handle_alert", None)
        if not callable(handler):
            return False
        target = urlsplit(page.url)
        if target.scheme != "https" or target.netloc != "hax.co.id":
            return False
        message = handler(accept=None, timeout=0)
        return isinstance(message, str) and message.strip() == ADBLOCK_MESSAGE
    except Exception:
        return False

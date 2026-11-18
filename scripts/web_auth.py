"""恢复 Telegram Web 会话，只读取固定业务会话。"""

import json
import re
import time
from typing import Any
from urllib.parse import urlsplit

from DrissionPage import ChromiumOptions, ChromiumPage

from scripts.public_report import record_check
from scripts.web_sessions import AuthenticationError, TELEGRAM_CLIENT, validate_snapshot

TELEGRAM_URL = "https://web.telegram.org/k/#@HaxTG_bot"
HAX_BOT_ID = "1967189265"


def browser_options(proxy: str | None = None) -> ChromiumOptions:
    """使用独立临时资料目录和本机调试端口。"""
    options = ChromiumOptions().auto_port()
    for value in (
        "--no-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--window-size=1920,1080",
    ):
        options.set_argument(value)
    if proxy:
        options.set_argument("--proxy-server", proxy)
        options.set_argument("--proxy-bypass-list", "<-loopback>")
    return options


class TelegramWeb:
    """只从 HaxTG_bot 接收业务码，不操作任何发送或回调控件。"""

    def __init__(self, page: Any, snapshot: object):
        # DrissionPage 页面方法返回动态 JSON，读取结果在此再次约束。
        self.page = page
        self.session = validate_snapshot(snapshot, expected_client=TELEGRAM_CLIENT)
        self.baseline = 0
        self.triggered_at: int | None = None
        self.consumed: set[int] = set()

    def restore(self, timeout: float = 90) -> None:
        """在空白资料目录的首个文档运行前注入最小状态。"""
        seed = json.dumps(self.session["account"], separators=(",", ":"))
        source = (
            "if (location.origin === 'https://web.telegram.org') {"
            "if (!localStorage.getItem('account1')) localStorage.setItem('account1', "
            + json.dumps(seed)
            + ");}"
        )
        try:
            self.page.run_cdp("Page.addScriptToEvaluateOnNewDocument", source=source)
            self.page.get(TELEGRAM_URL)
        except Exception:
            raise AuthenticationError("network_failed") from None
        deadline = time.monotonic() + timeout
        while True:
            status = self.read()
            if status["status"] == "ready":
                return
            if status["status"] == "login_required":
                raise AuthenticationError("telegram_session_invalid")
            if time.monotonic() >= deadline:
                raise AuthenticationError("telegram_peer_unverified")
            time.sleep(1)

    def read(self, include_new_text: bool = False) -> dict[str, Any]:
        """限定业务聊天；建立基线时不读取历史消息正文。"""
        try:
            location = urlsplit(self.page.url)
            if (
                location.scheme != "https"
                or location.hostname != "web.telegram.org"
                or location.path != "/k/"
                or location.username
                or location.password
                or location.port not in {None, 443}
            ):
                record_check("authentication", "telegram_web", "origin_invalid")
                raise AuthenticationError("telegram_peer_unverified")
            value = self.page.run_js(
                """
                const visible = element => element && element.getClientRects().length > 0;
                if (visible(document.querySelector('#auth-pages'))) return {status: 'login_required', messages: []};
                if (!visible(document.querySelector('#page-chats'))) return {status: 'client_loading', messages: []};
                const title = document.querySelector('#column-center .chat-info .peer-title[data-peer-id="1967189265"]');
                if (!visible(title)) return {status: 'peer_loading', messages: []};
                const identity = JSON.parse(localStorage.getItem('account1') || '{}');
                if (String(identity.userId) !== arguments[0] || identity.phone !== arguments[1]) return {status: 'wrong_account', messages: []};
                const messages = [...document.querySelectorAll('#column-center .bubble.is-in[data-peer-id="1967189265"][data-mid][data-timestamp]')].slice(-40).map(element => {
                    const id = element.dataset.mid, timestamp = element.dataset.timestamp;
                    const fresh = arguments[2] && Number(id) > arguments[3] && Number(timestamp) >= arguments[4];
                    const text = fresh ? (element.querySelector('.message')?.innerText || '').slice(0, 4096) : '';
                    return {id, timestamp, text};
                });
                return {status: 'ready', messages};
            """,
                self.session["account"]["userId"],
                self.session["account"]["phone"],
                include_new_text,
                self.baseline,
                self.triggered_at or 0,
            )
            if (
                not isinstance(value, dict)
                or set(value) != {"status", "messages"}
                or value["status"]
                not in {
                    "ready",
                    "client_loading",
                    "peer_loading",
                    "login_required",
                    "wrong_account",
                }
                or not isinstance(value["messages"], list)
                or len(value["messages"]) > 40
            ):
                raise ValueError("invalid_messages")
            record_check("authentication", "telegram_web", value["status"])
            if value["status"] == "wrong_account":
                raise AuthenticationError("telegram_session_invalid")
            for message in value["messages"]:
                if (
                    not isinstance(message, dict)
                    or set(message) != {"id", "timestamp", "text"}
                    or not isinstance(message["text"], str)
                    or len(message["text"]) > 4096
                ):
                    raise ValueError("invalid_message")
                for key in ("id", "timestamp"):
                    if (
                        not isinstance(message[key], str)
                        or not re.fullmatch(r"[0-9]{1,16}", message[key])
                        or int(message[key]) <= 0
                    ):
                        raise ValueError("invalid_message")
            return value
        except AuthenticationError:
            raise
        except Exception:
            record_check("authentication", "telegram_web", "unavailable")
            raise AuthenticationError("telegram_peer_unverified") from None

    def arm(self) -> None:
        """在网站触发验证码之前记录最新消息 ID 和时间。"""
        value = self.read()
        if value["status"] != "ready":
            raise AuthenticationError(
                "telegram_session_invalid"
                if value["status"] == "login_required"
                else "telegram_peer_unverified"
            )
        self.baseline = max(
            [self.baseline] + [int(message["id"]) for message in value["messages"]]
        )
        self.triggered_at = int(time.time())

    def wait_for_code(self, timeout: float = 150) -> str:
        """只接受本轮唯一的新业务码；不搜索无关会话或使用历史兜底。"""
        if self.triggered_at is None:
            raise AuthenticationError("renewal_code_unavailable")
        deadline = time.monotonic() + timeout
        while True:
            value = self.read(include_new_text=True)
            if value["status"] == "login_required":
                raise AuthenticationError("telegram_session_invalid")
            candidates = []
            now = int(time.time())
            for message in value["messages"]:
                mid, timestamp = int(message["id"]), int(message["timestamp"])
                if (
                    mid <= self.baseline
                    or mid in self.consumed
                    or timestamp < self.triggered_at
                    or timestamp > now
                ):
                    continue
                codes = renewal_codes(message["text"])
                candidates.extend((mid, code) for code in codes)
            if len(candidates) > 1:
                raise AuthenticationError("renewal_code_ambiguous")
            if candidates:
                mid, code = candidates[0]
                self.consumed.add(mid)
                self.baseline = max(self.baseline, mid)
                self.triggered_at = None
                return code
            if time.monotonic() >= deadline:
                raise AuthenticationError("renewal_code_unavailable")
            time.sleep(1)

    def close(self) -> None:
        """关闭本次创建的浏览器，不退出或撤销 Telegram 授权。"""
        try:
            self.page.quit(timeout=5, force=True, del_data=True)
        except Exception:
            pass


def renewal_codes(text: str) -> list[str]:
    """要求明确的验证码标签，不把年份或服务器数字当成验证码。"""
    found = re.findall(
        r"(?:your\s+code\s+is|(?:verification|renewal)\s+code(?:\s+is)?|code\s*[:：])\s*[:：]?\s*([A-Za-z0-9+/_=-]{4,1024})",
        text,
        re.IGNORECASE,
    )
    return list(
        dict.fromkeys(
            code
            for code in found
            if re.fullmatch(r"[0-9]{4,8}|[A-Za-z0-9+/_=-]{20,1024}", code)
        )
    )


def new_telegram_reader(snapshot: object, proxy: str | None = None) -> TelegramWeb:
    """创建与 Hax 分离的浏览器；恢复失败时立即清理。"""
    snapshot = validate_snapshot(snapshot, expected_client=TELEGRAM_CLIENT)
    page = None
    try:
        page = ChromiumPage(browser_options(proxy))
        page.set.timeouts(page_load=30, script=30)
    except Exception:
        if page is not None:
            try:
                page.quit(timeout=5, force=True, del_data=True)
            except Exception:
                pass
        raise AuthenticationError("browser_unavailable") from None
    reader = TelegramWeb(page, snapshot)
    try:
        reader.restore()
    except Exception:
        reader.close()
        raise
    return reader

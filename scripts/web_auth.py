"""恢复 Telegram Web 会话，只读取固定业务会话。"""

import json
import re
import time
from collections.abc import Callable, Iterator
from random import uniform
from typing import Any
from urllib.parse import urlsplit

from DrissionPage import ChromiumOptions, ChromiumPage

from scripts.hax_renewal.retry_report import RetryAttempt, RetryStep, start_retry
from scripts.hax_renewal.timing import remaining_time
from scripts.public_report import record_check
from scripts.web_sessions import AuthenticationError, TELEGRAM_CLIENT, validate_snapshot

TELEGRAM_URL = "https://web.telegram.org/k/#@HaxTG_bot"
HAX_BOT_ID = "1967189265"
_RESTORE_TIMEOUT = 480
_LOADING_STATES = {"client_loading", "peer_loading", "unavailable"}
_RESTORE_REOPEN_DELAYS = (45, 90, 120)
_CODE_REOPEN_DELAYS = (75,)
_MAX_REOPEN_DELAY = 120
_PENDING_NAVIGATION_URLS = {"", "about:blank", "chrome-error://chromewebdata/"}


class _TelegramReadUnavailable(AuthenticationError):
    """页面暂时无法读取，保留原来的会话和消息基线。"""

    def __init__(self) -> None:
        super().__init__("telegram_peer_unverified")


class _LoadingPageRecovery:
    """在同一等待窗口内，为卡住的会话保留每次重开后的观察时间。"""

    def __init__(self, deadline: float, delays: tuple[int, ...], observe_after: float) -> None:
        self.deadline = deadline
        self.delays = delays
        self.observe_after = observe_after
        self.reopens = 0
        self.loading_state: str | None = None
        self.next_reopen = 0.0
        self.observing: tuple[RetryAttempt, float] | None = None

    def observe(
        self, status: str, reopen: Callable[[str, float], RetryAttempt | None]
    ) -> None:
        if status == "ready":
            self.finish(recovered=True)
            self.loading_state = None
            return
        if status not in _LOADING_STATES or self.reopens >= len(self.delays):
            return

        now = time.monotonic()
        if status != self.loading_state:
            # 页面有进展时，从新阶段开始观察，避免打断正在加载的会话。
            self.loading_state = status
            delay = min(_MAX_REOPEN_DELAY, self.delays[self.reopens] * uniform(1, 1.1))
            self.next_reopen = now + delay
            return

        available = self.deadline - now - self.observe_after
        if now < self.next_reopen or available <= 0:
            return
        started = time.monotonic()
        attempt = reopen(status, available)
        if attempt is None:
            self.next_reopen = time.monotonic() + 5
            return
        self.finish(at=started)
        self.observing = (attempt, time.monotonic())
        self.reopens += 1
        self.loading_state = None

    def finish(self, *, recovered: bool = False, at: float | None = None) -> None:
        if self.observing is None:
            return
        attempt, started = self.observing
        finished = time.monotonic() if at is None else at
        attempt.observation_seconds = int(max(0, finished - started))
        attempt.recovered = recovered
        self.observing = None


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
        self._navigation_pending = False

    def restore(self, timeout: float = _RESTORE_TIMEOUT) -> None:
        """在空白资料目录的首个文档运行前注入最小状态。"""
        seed = json.dumps(self.session["account"], separators=(",", ":"))
        source = (
            "if (location.origin === 'https://web.telegram.org') {"
            "if (!localStorage.getItem('account1')) localStorage.setItem('account1', "
            + json.dumps(seed)
            + ");}"
        )
        window = remaining_time(timeout)
        deadline = time.monotonic() + window
        if not window and timeout != 0:
            raise AuthenticationError("timeout")
        try:
            self.page.run_cdp(
                "Page.addScriptToEvaluateOnNewDocument", source=source,
                _timeout=min(10, window) if window else 1,
            )
        except Exception:
            raise AuthenticationError("browser_unavailable") from None
        available = deadline - time.monotonic()
        if available <= 0 and timeout != 0:
            raise AuthenticationError("timeout")
        self._navigation_pending = True
        try:
            self.page.get(TELEGRAM_URL, retry=0, timeout=min(60, max(0.1, available)))
        except Exception:
            # 导航可能已完成，先观察原目标，再按加载恢复规则重开。
            pass
        recovery = _LoadingPageRecovery(deadline, _RESTORE_REOPEN_DELAYS, 60)
        self._wait_until_ready(deadline, recovery, snapshot_only=timeout == 0)

    def _wait_until_ready(
        self, deadline: float, recovery: _LoadingPageRecovery | None = None,
        *, snapshot_only: bool = False,
    ) -> dict[str, Any]:
        try:
            for value in self._observations(deadline, recovery, snapshot_only=snapshot_only):
                if value["status"] == "ready":
                    return value
            reason = "timeout" if not remaining_time(1) else "telegram_peer_unverified"
            raise AuthenticationError(reason)
        finally:
            if recovery is not None:
                recovery.finish()

    def _observations(
        self, deadline: float, recovery: _LoadingPageRecovery | None,
        *, include_new_text: bool = False, snapshot_only: bool = False,
    ) -> Iterator[dict[str, Any]]:
        read_failed = False
        read_retry = None
        while snapshot_only or time.monotonic() < deadline:
            if read_failed and read_retry is None:
                read_retry = start_retry(RetryStep.TELEGRAM_READ)
            value: dict[str, Any]
            try:
                timeout = min(5, max(0, deadline - time.monotonic()))
                value = self.read(include_new_text, timeout=timeout if not snapshot_only else 5)
            except _TelegramReadUnavailable:
                read_failed = True
                value = dict(status="unavailable", messages=[])
            if not snapshot_only and time.monotonic() >= deadline:
                break
            status = value["status"]
            if status == "login_required":
                raise AuthenticationError("telegram_session_invalid")
            if status != "unavailable" and read_retry is not None:
                read_retry.recovered = True
                read_retry = None
                read_failed = False
            if recovery is not None:
                recovery.observe(status, self._reopen_loading_page)
            yield value
            if snapshot_only:
                break
            time.sleep(min(1, max(0, deadline - time.monotonic())))

    def _read_location(self) -> str:
        """只在同一 Telegram 客户端或本次导航的空白页继续。"""
        try:
            current = self.page.url
        except Exception:
            raise _TelegramReadUnavailable() from None
        if self._navigation_pending and current in _PENDING_NAVIGATION_URLS:
            return current
        location = urlsplit(current)
        if (
            location.scheme != "https"
            or location.netloc not in {"web.telegram.org", "web.telegram.org:443"}
            or location.path != "/k/"
        ):
            record_check("authentication", "telegram_web", "origin_invalid")
            raise AuthenticationError("telegram_peer_unverified")
        return current

    def _reopen_loading_page(self, status: str, available: float) -> RetryAttempt | None:
        """只重开固定会话。业务码基线和已消费记录保存在当前读取器中。"""
        deadline = time.monotonic() + available
        try:
            current = self._read_location()
        except _TelegramReadUnavailable:
            # 暂时无法核对标签地址，保留原标签和本轮业务码请求。
            return None
        timeout = min(30, deadline - time.monotonic())
        if timeout <= 0:
            return None
        retry = start_retry(
            RetryStep.TELEGRAM_CLIENT
            if status in {"client_loading", "unavailable"}
            else RetryStep.TELEGRAM_CHAT
        )
        try:
            self._navigation_pending = True
            if current == TELEGRAM_URL:
                self.page.run_cdp("Page.reload", _timeout=timeout)
            else:
                self.page.get(TELEGRAM_URL, retry=0, timeout=timeout)
        except Exception:
            pass
        return retry

    def read(self, include_new_text: bool = False, *, timeout: float = 5) -> dict[str, Any]:
        """限定业务聊天；建立基线时不读取历史消息正文。"""
        try:
            if self._read_location() in _PENDING_NAVIGATION_URLS:
                raise _TelegramReadUnavailable()
            try:
                value = self.page.run_js(
                    """
                    if (location.origin !== 'https://web.telegram.org' || location.pathname !== '/k/') {
                        return {status: 'origin_invalid', messages: []};
                    }
                    const visible = element => element && element.getClientRects().length > 0;
                    if (visible(document.querySelector('#auth-pages'))) return {status: 'login_required', messages: []};
                    if (!visible(document.querySelector('#page-chats'))) return {status: 'client_loading', messages: []};
                    const identity = JSON.parse(localStorage.getItem('account1') || '{}');
                    if (String(identity.userId) !== arguments[0] || identity.phone !== arguments[1]) return {status: 'wrong_account', messages: []};
                    const title = document.querySelector('#column-center .chat-info .peer-title[data-peer-id="1967189265"]');
                    if (!visible(title)) return {status: 'peer_loading', messages: []};
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
                    timeout=timeout,
                )
            except Exception:
                raise _TelegramReadUnavailable() from None
            if value is None:
                raise _TelegramReadUnavailable()
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
                    "origin_invalid",
                }
                or not isinstance(value["messages"], list)
                or len(value["messages"]) > 40
            ):
                raise ValueError("invalid_messages")
            self._navigation_pending = False
            record_check("authentication", "telegram_web", value["status"])
            if value["status"] == "wrong_account":
                raise AuthenticationError("telegram_session_invalid")
            if value["status"] == "origin_invalid":
                raise AuthenticationError("telegram_peer_unverified")
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
        except _TelegramReadUnavailable:
            record_check("authentication", "telegram_web", "unavailable")
            raise
        except AuthenticationError:
            raise
        except Exception:
            record_check("authentication", "telegram_web", "unavailable")
            raise AuthenticationError("telegram_peer_unverified") from None

    def arm(self) -> None:
        """在网站触发验证码之前记录最新消息 ID 和时间。"""
        value = self._wait_until_ready(time.monotonic() + remaining_time(45))
        self.baseline = max(
            [self.baseline] + [int(message["id"]) for message in value["messages"]]
        )
        self.triggered_at = int(time.time())

    def wait_for_code(self, timeout: float = 150) -> str:
        """只接受本轮唯一的新业务码；不搜索无关会话或使用历史兜底。"""
        if self.triggered_at is None:
            raise AuthenticationError("renewal_code_unavailable")
        deadline = time.monotonic() + remaining_time(timeout)
        recovery = _LoadingPageRecovery(deadline, _CODE_REOPEN_DELAYS, 45)
        try:
            for value in self._observations(
                deadline, recovery, include_new_text=True, snapshot_only=timeout == 0
            ):
                candidates = []
                now = int(time.time())
                messages = value["messages"] if value["status"] == "ready" else []
                for message in messages:
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
            reason = "timeout" if not remaining_time(1) else "renewal_code_unavailable"
            raise AuthenticationError(reason)
        finally:
            recovery.finish()

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

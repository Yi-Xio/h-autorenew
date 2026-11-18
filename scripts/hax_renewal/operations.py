"""浏览器中的 Hax 续期操作，副作用与结果核对分别执行。"""

import time
from collections.abc import Callable
from time import monotonic
from typing import Any
from urllib.parse import urlsplit

from scripts.hax_renewal.retry_report import RetryStep, start_retry
from scripts.hax_renewal.timing import remaining_time, require_time
from scripts.hax_result import RenewalFeedback
from scripts.public_report import iso_date
from scripts.web_sessions import AuthenticationError, HAX_ORIGIN

HAX_VPS_RENEW_URL = f"{HAX_ORIGIN}/vps-renew"
HAX_VPS_RENEW_CODE_URL = f"{HAX_ORIGIN}/vps-renew-code"


def open_renewal_form(browser: Any) -> None:
    """打开请求业务码的表单并等待原有页面初始化时间。"""
    browser.open(HAX_VPS_RENEW_URL)
    time.sleep(5)


def fill_site_address(browser: Any) -> None:
    """填写初始表单要求的站点地址并核对当前值。"""
    # 填 web_address，调用与续期码相同的字段检查。
    _set_field(
        browser, "/vps-renew", '#web_address, input[name="web_address"]',
        "hax.co.id", RetryStep.SITE_ADDRESS,
    )


def accept_agreement(browser: Any) -> None:
    """只在尚未勾选时操作，并核对协议状态。"""
    for attempt in range(2):
        require_time()
        retry = start_retry(RetryStep.AGREEMENT) if attempt else None
        try:
            if browser.execute_script("""
                const input = document.querySelector('input[name="agreement"]');
                if (!input) return false;
                if (!input.checked) input.click();
                return input.checked;
            """) is True:
                if retry:
                    retry.recovered = True
                return
        except Exception:
            pass
    raise AuthenticationError("hax_page_unverified")


def click_code_request(browser: Any) -> bool:
    """返回是否尝试请求；浏览器异常时由调用方继续核实本轮响应。"""
    return browser.execute_script(r"""
        if (location.origin !== 'https://hax.co.id' ||
                location.pathname.replace(/\/$/, '') !== '/vps-renew') return false;
        const btn = document.querySelector('button[name="submit_button"]');
        const address = document.querySelector('#web_address, input[name="web_address"]');
        const agreement = document.querySelector('input[name="agreement"]');
        if (!btn || btn.disabled || address?.value !== 'hax.co.id' || !agreement?.checked ||
                document.querySelector('#response a[href*="renew-code"]')) return false;
        btn.click();
        return true;
    """) is not False


def is_hax_url(url: str, path: str | None = None) -> bool:
    """确认安全重开的地址属于 Hax 及预期业务路由。"""
    try:
        address = urlsplit(url)
        return (
            address.scheme == "https"
            and address.netloc == "hax.co.id"
            and (path is None or address.path.rstrip("/") == path)
        )
    except ValueError:
        return False


def can_reopen_hax_page(current: str, target: str) -> bool:
    """允许重开已核对的目标路由或本次导航留下的浏览器空白、错误页。"""
    return is_hax_url(target) and (
        is_hax_url(current, urlsplit(target).path.rstrip("/"))
        or current in {"", "about:blank", "chrome-error://chromewebdata/"}
    )


def wait_for_code_link(browser: Any) -> str:
    """在原响应页等待本次码页入口，并核对链接来源。"""
    deadline = monotonic() + remaining_time(30)
    while monotonic() < deadline:
        try:
            link = browser.execute_script("""
                const response = document.getElementById('response');
                if (!response || location.origin !== 'https://hax.co.id') return null;
                return response.querySelector('a[href*="renew-code"]')?.href || null;
            """)
        except Exception:
            link = None
        if monotonic() >= deadline:
            break
        if isinstance(link, str):
            if is_hax_url(link, "/vps-renew-code"):
                return link
            raise AuthenticationError("hax_page_unverified")
        time.sleep(min(0.5, max(0, deadline - monotonic())))
    require_time()
    raise AuthenticationError("renewal_code_unavailable")


def open_code_page(browser: Any, target: str) -> None:
    """打开从本次请求响应中确认的码页入口。"""
    browser.open(target)


def wait_for_code_page(browser: Any) -> bool:
    """等待本轮码页导航完成，页面控件将在 WARP 浏览器中核对。"""
    deadline = monotonic() + remaining_time(20)
    while monotonic() < deadline:
        try:
            current = browser.get_current_url()
            if monotonic() < deadline and is_hax_url(current, "/vps-renew-code"):
                return True
        except Exception:
            pass
        time.sleep(min(0.5, max(0, deadline - monotonic())))
    return False


def position_code_form(browser: Any) -> None:
    """滚动到码页的表单区域。"""
    try:
        browser.execute_script("""
            (function() {
                var el = document.querySelector('input[name="code"]')
                      || document.querySelector('input[type="text"]')
                      || document.querySelector('.col-sm-3 img')
                      || document.querySelector('form');
                if (el) el.scrollIntoView({block: "center", behavior: "instant"});
            })()
        """)
        time.sleep(0.5)
    except Exception:
        pass


def fill_renewal_code(browser: Any, code: str) -> None:
    """把本轮业务码填入最终表单，并核对写入结果。"""
    # reCAPTCHA 解完后，输入续期验证码（TG bot 发来的）
    _set_field(
        browser,
        "/vps-renew-code",
        'input[name="code"], input[name="verification_code"], input[name="otp"]',
        code,
        RetryStep.RENEWAL_CODE,
    )


def _set_field(browser: Any, path: str, selector: str, value: str, step: RetryStep) -> None:
    for attempt in range(2):
        require_time()
        retry = start_retry(step) if attempt else None
        try:
            if not is_hax_url(browser.get_current_url(), path):
                raise AuthenticationError("hax_page_unverified")
            status = browser.execute_script(r"""
                if (window !== window.top || location.origin !== 'https://hax.co.id' ||
                        location.pathname.replace(/\/$/, '') !== arguments[2]) return 'wrong_page';
                const input = document.querySelector(arguments[0]);
                const value = arguments[1];
                if (!input) return false;
                if (input.value !== value) {
                    input.focus();
                    input.value = value;
                    input.dispatchEvent(new Event('input', {bubbles: true}));
                    input.dispatchEvent(new Event('change', {bubbles: true}));
                }
                return input.value === value;
            """, selector, value, path)
        except (AuthenticationError, TimeoutError):
            raise
        except Exception:
            continue
        if status == "wrong_page":
            break
        if status is True:
            if retry:
                retry.recovered = True
            return
    raise AuthenticationError("hax_page_unverified")


def position_final_form(browser: Any) -> None:
    """滚动到最终表单区域供现有截图逻辑使用。"""
    # 滚动到 Renew VPS 按钮区域并截图
    try:
        browser.execute_script("""
            (function() {
                var els = document.querySelectorAll('label, h2, h3, h4, div, span, p');
                for (var i = 0; i < els.length; i++) {
                    if (els[i].textContent.trim() === 'Renew VPS') {
                        els[i].scrollIntoView({block: "start", behavior: "instant"});
                        return;
                    }
                }
                window.scrollTo(0, document.body.scrollHeight);
            })()
        """)
        time.sleep(0.5)
    except Exception:
        pass


def align_final_button(browser: Any) -> None:
    """滚动最终续期按钮到视口中心。"""
    browser.execute_script("""
        (function() {
            var btn = document.querySelector('button[name="submit_button"], button[data-callback="onSubmit"]');
            if (btn) btn.scrollIntoView({block: 'center', behavior: 'instant'});
        })();
    """)
    time.sleep(1)


def observe_submission(browser: Any) -> None:
    """以当前页面创建本轮最终提交的观察器。"""
    previous_feedback = getattr(browser, "renewal_submission", None)
    if previous_feedback:
        previous_feedback.close()
    browser.renewal_submission = None
    browser.renewal_submission = RenewalFeedback(browser.page)


def click_final_button(browser: Any) -> bool:
    """继续找按钮只发生在调用点击之前。"""
    # 只用真实鼠标点击，避免通过脚本提交或发送替代请求。
    for locator in (
        'css:button[name="submit_button"]',
        'css:button[data-callback="onSubmit"]',
        "css:button.btn-primary",
        'css:button[type="submit"]',
        'css:input[type="submit"]',
    ):
        require_time()
        try:
            element = browser.page.ele(locator, timeout=min(2, remaining_time(2)))
        except Exception:
            continue
        if not element:
            continue
        require_time()
        if not is_hax_url(browser.get_current_url(), "/vps-renew-code"):
            raise AuthenticationError("hax_page_unverified")
        browser.renewal_submission.activate()
        timeout = remaining_time(12)
        if not timeout:
            raise TimeoutError("hax_operation_deadline")
        # 调用点击前已标记本轮。调用异常无法证明网络请求未发出。
        try:
            clicked = element.click(timeout=timeout)
        except Exception:
            print("  [WARN] 最终按钮点击响应不可用，继续核实本轮反馈")
            return True
        # 当前浏览器驱动仅在未发出点击时明确返回 False。
        # 此时由调用方关闭旧观察器，再有限地准备按钮。
        return clicked is not False
    return False


def read_submission_feedback(browser: Any) -> tuple[dict[str, str], bool]:
    """在同一轮观察中读取页面和原生弹窗，结束后释放观察器。"""
    tracker = getattr(browser, "renewal_submission", None)
    feedback = dict(status="unknown", date="", source="none", reason="none")
    attempted = bool(tracker and tracker.active)
    deadline = monotonic() + remaining_time(40)
    try:
        if tracker is not None and attempted:
            for attempt in range(6):
                drain_result_alerts(browser)
                try:
                    feedback = tracker.read()
                except Exception:
                    pass
                if feedback["status"] != "unknown" or monotonic() >= deadline:
                    break
                if attempt < 5:
                    time.sleep(min(8, max(0, deadline - monotonic())))
    finally:
        close_submission(browser)
    return feedback, attempted


def close_submission(browser: Any) -> None:
    """只撤销本轮反馈观察器。"""
    tracker = getattr(browser, "renewal_submission", None)
    try:
        if tracker:
            tracker.close()
    finally:
        browser.renewal_submission = None


def drain_result_alerts(browser: Any) -> None:
    """接受阻塞当前页面的结果弹窗，分类由本轮观察器保留。"""
    # Hax 会用原生 alert 显示结果，未处理时后续页面读取会被阻塞。
    interval = remaining_time(0.5)
    if interval:
        try:
            browser.page.handle_alert(accept=True, timeout=interval)
        except Exception:
            pass


def capture_final_info(
    browser: Any,
    result: dict[str, Any],
    old_valid_until: str,
    read_vps_info: Callable[[Any, int], tuple[list[dict[str, str]], str, str | None]],
) -> None:
    """无论续期成败，最后回到 VPS info 页面并以该页截图/到期日为准。"""
    result["info_valid_until"] = ""
    result["screenshot"] = None
    result["vps_info_screenshot"] = ""
    for attempt in range(2):
        if not remaining_time(1):
            return
        retry = start_retry(RetryStep.FINAL_INFO) if attempt else None
        try:
            servers, error, screenshot = read_vps_info(browser, result.get("_idx", 0))
        except Exception:
            continue
        sid = str(result.get("server_id", ""))
        current = next((server for server in servers if str(server.get("id", "")) == sid), None)
        if current is None:
            if error:
                continue
            return
        if screenshot:
            result["screenshot"] = screenshot
            result["vps_info_screenshot"] = screenshot
        observed_date = iso_date(current.get("valid_until"))
        if observed_date is not None:
            new_valid = observed_date.isoformat()
            result["info_valid_until"] = new_valid
            result["new_valid_until"] = new_valid
            result["expiry_info"] = (
                f"{old_valid_until} → {new_valid}"
                if old_valid_until
                else f"新到期日: {new_valid}"
            )
            print(f"  [INFO] info 页面到期日对比: {old_valid_until} → {new_valid}")
        for field in ("status", "location", "hostname"):
            result[field] = current.get(field, result.get(field, ""))
        if observed_date is not None:
            if retry:
                retry.recovered = True
            return

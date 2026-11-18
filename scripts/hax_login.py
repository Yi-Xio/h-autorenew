"""仅复用 Hax 已有网站授权，建立本轮站点会话。"""

import json
import re
import time
from typing import Any
from urllib.parse import urlencode, urlsplit

from scripts.browser_setup import has_adblock_dialog, install_hax_rules
from scripts.hax_renewal.retry_report import RetryStep, start_retry
from scripts.hax_renewal.timing import remaining_time, require_time
from scripts.public_report import record_check, record_navigation
from scripts.web_sessions import (
    HAX_CLIENT,
    HAX_LOGIN_BOT,
    HAX_LOGIN_ORIGIN,
    HAX_ORIGIN,
    AuthenticationError,
    validate_snapshot,
)

# DrissionPage 的页面对象和 CDP 返回动态数据，在各个边界单独校验。
Page = Any


def _evaluate(
    page: Page, script: str, *arguments: object,
    deadline: float | None = None, timeout: float = 5,
) -> Any:
    available = require_time(timeout)
    if deadline is not None:
        available = min(available, deadline - time.monotonic())
        if available <= 0:
            raise AuthenticationError("browser_unavailable")
    expression = (
        "(function(){" + script + "}).apply(null," + json.dumps(list(arguments)) + ")"
    )
    try:
        result = page.run_cdp(
            "Runtime.evaluate",
            expression=expression,
            awaitPromise=True,
            returnByValue=True,
            _timeout=available,
        )
    except Exception:
        require_time()
        raise AuthenticationError("browser_unavailable") from None
    require_time()
    if deadline is not None and time.monotonic() >= deadline:
        raise AuthenticationError("browser_unavailable")
    if (
        not isinstance(result, dict)
        or result.get("exceptionDetails")
        or not isinstance(result.get("result"), dict)
    ):
        raise AuthenticationError("browser_unavailable")
    return result["result"].get("value")


def hax_status(browser: Page, *, deadline: float | None = None) -> str:
    """区分受保护页面、登录页、访问验证及未知页面。"""
    try:
        if has_adblock_dialog(browser.page):
            return "adblock"
        status = _evaluate(
            browser.page,
            r"""
            if (location.origin !== 'https://hax.co.id') return 'unknown';
            if ((document.body?.innerText || '').trim() === 'Please disable AdBlock to access this site!') return 'adblock';
            if (document.title.toLowerCase().includes('just a moment') ||
                document.querySelector('#challenge-running, #challenge-stage')) return 'challenge';
            if (document.querySelector('[data-telegram-login]') ||
                location.pathname.replace(/\/$/, '') === '/login') return 'login_required';
            const logout = [...document.querySelectorAll('a[href]')].some(element => {
                const target = new URL(element.href);
                return target.origin === location.origin && target.pathname === '/logout';
            });
            const info = [...document.querySelectorAll('h1,h2,h3,h4,h5')]
                .some(element => element.textContent.trim() === 'VPS Information');
            return logout && info && location.pathname.replace(/\/$/, '') === '/vps-info' ? 'authenticated' : 'unknown';
        """,
            deadline=deadline,
        )
        return (
            status
            if isinstance(status, str)
            and status
            in {"authenticated", "login_required", "challenge", "unknown", "adblock"}
            else "unknown"
        )
    except TimeoutError:
        raise
    except Exception:
        return "unknown"


def wait_hax_navigation(
    browser: Page, target_url: str, timeout: float = 60, *, attempt_challenge: bool = True
) -> None:
    """核实已解析的目标业务页；资源仍加载时额外要求本轮规则标识一致。"""
    target = urlsplit(target_url)
    if target.scheme != "https" or target.netloc != "hax.co.id":
        raise AuthenticationError("hax_page_unverified")
    expected_path = target.path.rstrip("/")
    if expected_path not in {"", "/vps-info", "/vps-renew", "/vps-renew-code"}:
        raise AuthenticationError("hax_page_unverified")
    started = time.monotonic()
    deadline = started + remaining_time(timeout)
    challenged = False
    previous = None
    last_recorded = started
    while True:
        require_time()
        if has_adblock_dialog(browser.page):
            raise AuthenticationError("adblock_detected")
        try:
            observation = _evaluate(
                browser.page,
                r"""
                const path=location.pathname.replace(/\/$/, '');
                const route={'':'home','/vps-info':'info','/vps-renew':'renew','/vps-renew-code':'renew_code','/login':'login'};
                const expected_origin=location.origin==='https://hax.co.id';
                if (!expected_origin) return {expected_origin:false,path_match:false,
                    logout_link:false,login_page:false,code_input:false,submit_button:false,
                    info_heading:false,rules_ready:false,dom_ready:false,adblock:false,challenge:false,
                    document_state:document.readyState,current_route:'other',status:'unknown'};
                const adblock=expected_origin && (document.body?.innerText || '').trim()==='Please disable AdBlock to access this site!';
                const challenge=expected_origin && (document.title.toLowerCase().includes('just a moment') ||
                    !!document.querySelector('#challenge-running,#challenge-stage'));
                const logout = [...document.querySelectorAll('a[href]')].some(element => {
                    const url = new URL(element.href);
                    return url.origin === location.origin && url.pathname === '/logout';
                });
                const login=!!document.querySelector('[data-telegram-login]') || path==='/login';
                const input=!!document.querySelector('input[name="code"],input[name="verification_code"],input[name="otp"]');
                const button=!!document.querySelector('button[name="submit_button"],button[data-callback="onSubmit"],button.btn-primary,button[type="submit"],input[type="submit"]');
                const info=[...document.querySelectorAll('h1,h2,h3,h4,h5')].some(element => element.textContent.trim()==='VPS Information');
                const rules=window[Symbol.for('h-autorenew.hax-rules.v1')];
                // interactive 期间仍可能有 defer 脚本未完成；确认 DOMContentLoaded 已结束。
                const dom_ready=document.readyState==='complete' ||
                    (performance.getEntriesByType('navigation')[0]?.domContentLoadedEventEnd || 0)>0;
                const snapshot={expected_origin,path_match:path===arguments[0],logout_link:logout,
                    login_page:login,code_input:input,submit_button:button,info_heading:info,
                    rules_ready:!!rules && rules.fetch===window.fetch && rules.listener===EventTarget.prototype.addEventListener,
                    dom_ready,adblock,challenge,document_state:document.readyState,current_route:route[path] || 'other'};
                let status='unknown';
                if (!expected_origin) status='unknown';
                else if (adblock) status='adblock';
                else if (challenge) status='challenge';
                else if (document.readyState==='loading' || !dom_ready ||
                    (document.readyState!=='complete' && !snapshot.rules_ready)) status='loading';
                else if (login) status='login_required';
                else if (path!==arguments[0] || !logout) status='unknown';
                else if (arguments[0] === '/vps-renew-code') {
                    status=input && button ? 'ready' : 'unknown';
                } else if (arguments[0] === '/vps-renew') {
                    const address=!!document.querySelector('#web_address,input[name="web_address"]');
                    const agreement=!!document.querySelector('input[name="agreement"]');
                    const request=!!document.querySelector('button[name="submit_button"]');
                    status=address && agreement && request ? 'ready' : 'unknown';
                } else if (arguments[0] === '/vps-info') {
                    status=info ? 'ready' : 'unknown';
                } else status='ready';
                return {...snapshot,status};
                """,
                expected_path,
                deadline=deadline if timeout else None,
            )
        except AuthenticationError:
            observation = dict(
                status="unavailable",
                document_state="unavailable",
                current_route="other",
                **dict.fromkeys(
                    (
                        "expected_origin",
                        "path_match",
                        "logout_link",
                        "login_page",
                        "code_input",
                        "submit_button",
                        "info_heading",
                        "rules_ready",
                        "dom_ready",
                        "adblock",
                        "challenge",
                    ),
                    False,
                ),
            )
        if not isinstance(observation, dict):
            raise AuthenticationError("hax_page_unverified")
        now = time.monotonic()
        if previous != observation or now - last_recorded >= 15 or now >= deadline:
            record_navigation(
                dict(
                    observation,
                    expected_route={
                        "": "home",
                        "/vps-info": "info",
                        "/vps-renew": "renew",
                        "/vps-renew-code": "renew_code",
                    }[expected_path],
                    elapsed_seconds=max(0, int(now - started)),
                )
            )
            previous, last_recorded = observation, now
        status = observation.get("status")
        if status == "ready" and (not timeout or now < deadline):
            return
        if status == "adblock":
            raise AuthenticationError("adblock_detected")
        if status == "login_required":
            raise AuthenticationError("hax_session_invalid")
        if status == "challenge":
            if not attempt_challenge:
                raise AuthenticationError("access_blocked")
            challenged = True
        if time.monotonic() >= deadline:
            raise AuthenticationError(
                "access_blocked" if challenged else "hax_page_unverified"
            )
        if status == "challenge":
            try:
                browser.click_turnstile()
            except Exception:
                pass
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))


def read_existing_authorization(
    page: Page,
    expected_user_id: str,
    request_access: str | None = None,
    timeout: float = 45,
) -> dict[str, str]:
    """只查询已有网站授权，不申请权限或创建手机授权请求。"""
    if (
        not isinstance(expected_user_id, str)
        or not re.fullmatch(r"[1-9][0-9]{0,19}", expected_user_id)
        or request_access is not None
        and not isinstance(request_access, str)
        or request_access not in {None, "", "write"}
    ):
        record_check("authentication", "oauth_widget", "invalid")
        raise AuthenticationError("hax_widget_unverified")
    query = dict(origin=HAX_ORIGIN, return_to=HAX_ORIGIN + "/login")
    if request_access:
        query["request_access"] = request_access
    widget_url = HAX_LOGIN_ORIGIN + "/embed/" + HAX_LOGIN_BOT + "?" + urlencode(query)
    widget = None
    try:
        window = require_time(timeout or 30)
        started = time.monotonic()
        deadline = started + window
        probe_deadline = deadline if timeout else None
        reopen_at = started + window / 2
        record_check("authentication", "oauth_widget", "waiting")
        widget = page.browser.new_tab()
        initial_timeout = min(require_time(30), deadline - time.monotonic(), window / 2)
        if initial_timeout <= 0:
            raise AuthenticationError("hax_widget_unverified")
        try:
            widget.get(widget_url, retry=0, timeout=initial_timeout)
        except Exception:
            pass
        retry = read_retry = None
        unavailable = False
        while True:
            if unavailable and read_retry is None and time.monotonic() < deadline:
                read_retry = start_retry(RetryStep.HAX_AUTH_READ)
            try:
                ready = _evaluate(
                    widget,
                    """
                    if (location.origin !== 'https://oauth.telegram.org' ||
                        location.pathname !== '/embed/LoginHaxBot') return false;
                    if (typeof TWidgetLogin !== 'object' || !TWidgetLogin) return 'loading';
                    return String(TWidgetLogin.botId) === '5938743585' &&
                        TWidgetLogin.params?.origin === 'https://hax.co.id' ? true : 'invalid';
                    """,
                    deadline=probe_deadline,
                )
            except AuthenticationError:
                if not timeout:
                    raise
                ready = None
                unavailable = True
            else:
                if read_retry is not None and ready in (True, "loading"):
                    read_retry.recovered = True
                    read_retry = None
                    unavailable = False
            if ready is True:
                if retry:
                    retry.recovered = True
                record_check("authentication", "oauth_widget", "ready")
                break
            if ready == "invalid":
                record_check("authentication", "oauth_widget", "invalid")
                raise AuthenticationError("hax_widget_unverified")
            now = time.monotonic()
            if not timeout or now >= deadline:
                record_check("authentication", "oauth_widget", "missing")
                raise AuthenticationError("hax_widget_unverified")
            if ready == "loading" and retry is None and now >= reopen_at:
                reopen_timeout = require_time(min(30, deadline - now))
                retry = start_retry(RetryStep.HAX_AUTH_WIDGET)
                try:
                    widget.get(widget_url, retry=0, timeout=reopen_timeout)
                except Exception:
                    pass
            time.sleep(min(0.5, max(0, deadline - time.monotonic())))
        record_check("authentication", "authorization", "reading")
        request_timeout = require_time(15)
        if probe_deadline is not None:
            request_timeout = min(request_timeout, probe_deadline - time.monotonic())
            if request_timeout <= 0:
                raise AuthenticationError("hax_widget_unverified")
        result = _evaluate(
            widget,
            """
            if (location.origin !== 'https://oauth.telegram.org' ||
                location.pathname !== '/embed/LoginHaxBot' ||
                typeof TWidgetLogin !== 'object' ||
                String(TWidgetLogin.botId) !== '5938743585' ||
                TWidgetLogin.params.origin !== 'https://hax.co.id') return {_widget_context_invalid:true};
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), arguments[0]);
            return fetch('/auth/get?bot_id=5938743585', {
                method: 'POST', credentials: 'include', signal: controller.signal,
                headers: {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8', 'X-Requested-With': 'XMLHttpRequest'},
                body: TWidgetLogin.paramsEncoded
            }).then(async response => {
                const target = new URL(response.url);
                if (!response.ok || target.origin !== 'https://oauth.telegram.org' || target.pathname !== '/auth/get') return null;
                return await response.json();
            }).catch(() => null).finally(() => clearTimeout(timer));
            """,
            int(request_timeout * 1000),
            deadline=probe_deadline,
            timeout=16,
        )
        if not isinstance(result, dict):
            record_check("authentication", "authorization", "unavailable")
            raise AuthenticationError("network_failed")
        if result.get("_widget_context_invalid") is True:
            record_check("authentication", "oauth_widget", "invalid")
            raise AuthenticationError("hax_widget_unverified")
        user = result.get("user")
        if not user:
            record_check("authentication", "authorization", "required")
            raise AuthenticationError("hax_authorization_required")
        if result.get("origin") != HAX_ORIGIN or not isinstance(user, dict):
            record_check("authentication", "authorization", "invalid")
            raise AuthenticationError("hax_widget_unverified")
        if str(user.get("id")) != expected_user_id:
            record_check("authentication", "authorization", "mismatch")
            raise AuthenticationError("hax_account_mismatch")
        allowed = {
            "id",
            "first_name",
            "last_name",
            "username",
            "photo_url",
            "auth_date",
            "hash",
        }
        if not {"id", "auth_date", "hash"} <= set(user) or set(user) - allowed:
            raise AuthenticationError("hax_callback_unverified")
        if any(
            type(value) not in {str, int} or len(str(value)) > 4096
            for value in user.values()
        ):
            raise AuthenticationError("hax_callback_unverified")
        if not isinstance(user["hash"], str) or not re.fullmatch(
            r"[a-fA-F0-9]{64}", user["hash"]
        ):
            raise AuthenticationError("hax_callback_unverified")
        if (
            not re.fullmatch(r"[0-9]{1,12}", str(user["auth_date"]))
            or not -30 <= time.time() - int(user["auth_date"]) <= 300
        ):
            raise AuthenticationError("hax_callback_unverified")
        record_check("authentication", "authorization", "ready")
        # 保留组件原始签名字段；签名处理交给 Hax，不自行生成或修改签名。
        return {key: str(value) for key, value in user.items()}
    except (AuthenticationError, TimeoutError):
        raise
    except Exception:
        raise AuthenticationError("network_failed") from None
    finally:
        if widget is not None:
            try:
                widget.close()
            except Exception:
                pass


def restore_hax(browser: Page, snapshot: object, timeout: float = 60) -> None:
    """在本轮隔离浏览器中复用已有授权，不发起新的 Telegram 授权。"""
    saved = validate_snapshot(snapshot, expected_client=HAX_CLIENT)
    require_time()
    install_hax_rules(browser.page)
    cookies = []
    for value in saved["cookies"]:
        cookie = dict(value)
        if cookie["expires"] == -1:
            cookie.pop("expires")
        cookies.append(cookie)
    login_url = HAX_ORIGIN + "/login"
    started = time.monotonic()
    window = require_time(timeout or 30)
    deadline = started + window
    probe_deadline = deadline if timeout else None
    reopened = False
    retry = None
    cookie_timeout = require_time(5)
    try:
        browser.page.run_cdp("Network.setCookies", cookies=cookies, _timeout=cookie_timeout)
    except Exception:
        raise AuthenticationError("browser_unavailable") from None
    try:
        browser.open(login_url, timeout=window)
    except TimeoutError:
        raise
    except Exception:
        if not timeout or time.monotonic() >= deadline:
            raise AuthenticationError("browser_unavailable") from None
        retry = start_retry(RetryStep.HAX_LOGIN)
        try:
            browser.open(login_url, timeout=deadline - time.monotonic())
            reopened = True
        except TimeoutError:
            raise
        except Exception:
            raise AuthenticationError("browser_unavailable") from None
    challenged = False
    record_check("authentication", "site_widget", "waiting")
    while True:
        require_time()
        if has_adblock_dialog(browser.page):
            raise AuthenticationError("adblock_detected")
        try:
            configuration = _evaluate(
                browser.page,
                """
                if (location.origin !== 'https://hax.co.id') return null;
                if ((document.body?.innerText || '').trim() === 'Please disable AdBlock to access this site!') return 'adblock';
                const element = document.querySelector('[data-telegram-login="LoginHaxBot"]');
                if (!element) return null;
                return {bot: element.getAttribute('data-telegram-login'),
                    callback: new URL(element.getAttribute('data-auth-url'), location.href).href,
                    request_access: element.getAttribute('data-request-access')};
                """,
                deadline=probe_deadline,
            )
        except AuthenticationError:
            configuration = None
        if configuration == "adblock":
            raise AuthenticationError("adblock_detected")
        if configuration is not None:
            break
        if hax_status(browser, deadline=probe_deadline) == "challenge":
            challenged = True
            try:
                browser.click_turnstile()
            except Exception:
                pass
        now = time.monotonic()
        if not timeout or now >= deadline:
            record_check(
                "authentication",
                "site_widget",
                "challenge" if challenged else "missing",
            )
            raise AuthenticationError(
                "access_blocked" if challenged else "hax_widget_unverified"
            )
        if not reopened and not challenged and window >= 2 and now >= started + window / 2:
            reopened = True
            if hax_status(browser, deadline=probe_deadline) == "login_required":
                retry = start_retry(RetryStep.HAX_LOGIN)
                try:
                    browser.open(login_url, timeout=deadline - time.monotonic())
                except TimeoutError:
                    raise
                except Exception:
                    pass
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))
    if (
        not isinstance(configuration, dict)
        or set(configuration) != {"bot", "callback", "request_access"}
        or configuration["bot"] != HAX_LOGIN_BOT
        or not isinstance(configuration["callback"], str)
    ):
        record_check("authentication", "site_widget", "invalid")
        raise AuthenticationError("hax_widget_unverified")
    record_check("authentication", "site_widget", "ready")
    try:
        callback = urlsplit(configuration["callback"])
        valid = (
            callback.scheme == "https"
            and callback.hostname == "hax.co.id"
            and callback.port in {None, 443}
            and not callback.username
            and not callback.password
            and callback.path.rstrip("/") == "/authentication"
            and not callback.query
            and not callback.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        record_check("authentication", "callback", "invalid")
        raise AuthenticationError("hax_callback_unverified")
    if retry:
        retry.recovered = True
    user = read_existing_authorization(
        browser.page,
        saved["account"]["userId"],
        configuration["request_access"],
        timeout,
    )
    try:
        sent = _evaluate(
            browser.page,
            "if (location.origin !== 'https://hax.co.id') return false; location.assign(arguments[0]); return true;",
            configuration["callback"] + "?" + urlencode(user),
        )
    except AuthenticationError:
        # 回调已交给浏览器时，响应丢失也可能已经登录成功。
        record_check("authentication", "callback", "unavailable")
    else:
        if sent is not True:
            record_check("authentication", "callback", "invalid")
            raise AuthenticationError("hax_callback_unverified")
        record_check("authentication", "callback", "sent")
    deadline = time.monotonic() + require_time(timeout or 5)
    while True:
        status = hax_status(browser, deadline=deadline if timeout else None)
        if status == "authenticated":
            record_check("authentication", "callback", "ready")
            return
        if status == "adblock":
            raise AuthenticationError("adblock_detected")
        if status == "challenge":
            challenged = True
            try:
                browser.click_turnstile()
            except Exception:
                pass
        if not timeout or time.monotonic() >= deadline:
            record_check(
                "authentication",
                "callback",
                "challenge" if challenged else "unavailable",
            )
            raise AuthenticationError(
                "access_blocked" if challenged else "hax_page_unverified"
            )
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))

"""记录本轮真实提交后的 Hax 成功提示，不扫描整页源码。"""

import json
import secrets
from datetime import datetime
from typing import Any

# DrissionPage 的 CDP 接口返回动态对象，结果在读取时逐项校验。
Page = Any

_OBSERVE = r"""
(function(marker, initialize) {
    if (window !== window.top || location.origin !== 'https://hax.co.id') return false;
    const key = '__hax_renewal_feedback';
    const load = () => {
        try { return JSON.parse(sessionStorage.getItem(key)); } catch (_) { return null; }
    };
    if (initialize) {
        const path = location.pathname.replace(/\/$/, '');
        if (path !== '/vps-renew-code' || !document.querySelector('input[name="code"],input[name="verification_code"],input[name="otp"]')) return false;
        sessionStorage.setItem(key, JSON.stringify({marker, active:false, status:'unknown', date:'', source:'none', reason:'none'}));
    }
    const current = load();
    if (!current || current.marker !== marker) return false;
    if (window[key]) window[key].close();
    const record = (text, source, failed) => {
        const state = load();
        if (!state || state.marker !== marker || state.active !== true) return;
        const message = String(text).replace(/\s+/g, ' ').trim();
        const success = message.match(/\bYour VPS has been renewed until\s+([A-Za-z]+ \d{1,2}, \d{4})\b/i);
        if (!failed && success) {
            Object.assign(state, {status:'success', date:success[1], source, reason:'none'});
        } else if (failed || /verification code is wrong|captcha.*(?:wrong|invalid|failed|error)|(?:renewal|verification) code.*(?:invalid|expired)/i.test(message)) {
            const reason = /captcha/i.test(message) ? 'captcha' : /code/i.test(message) ? 'code' : 'rejected';
            Object.assign(state, {status:'failed', date:'', source, reason});
        } else {
            return;
        }
        sessionStorage.setItem(key, JSON.stringify(state));
    };
    const selector = '#response .alert-success,#response .alert-danger,#response .alert-warning';
    const visible = element => {
        if (!element.getClientRects().length) return false;
        for (let current = element; current; current = current.parentElement) {
            const style = getComputedStyle(current);
            if (style.visibility === 'hidden' || style.display === 'none' || Number(style.opacity) === 0) return false;
        }
        return true;
    };
    const previous = new Map(initialize ? [...document.querySelectorAll(selector)].map(element => [element, visible(element)]) : []);
    const scan = (changes = []) => {
        const all = [...document.querySelectorAll(selector)];
        const messages = all.filter(element => visible(element) && (
            !previous.has(element) || previous.get(element) === false ||
            changes.some(change => change.target === element || element.contains(change.target))
        ));
        const failure = messages.find(element => element.matches('.alert-danger,.alert-warning'));
        if (failure) record(failure.innerText, 'page', true);
        else for (const element of messages) record(element.innerText, 'page', false);
        for (const element of all) previous.set(element, visible(element));
    };
    const observer = new MutationObserver(records => {
        const related = [...records].some(change => {
            const target = change.target.nodeType === 1 ? change.target : change.target.parentElement;
            if (target && target.closest('#response')) return true;
            return [...change.addedNodes].some(node => node.nodeType === 1 && (node.id === 'response' || node.querySelector('#response')));
        });
        if (related) scan(records);
    });
    const originalAlert = window.alert;
    const observedAlert = function(message) {
        record(message, 'dialog', false);
        return originalAlert.apply(this, arguments);
    };
    window.alert = observedAlert;
    window[key] = {
        marker,
        close() {
            observer.disconnect();
            if (window.alert === observedAlert) window.alert = originalAlert;
        }
    };
    const observe = () => {
        observer.observe(document.documentElement, {subtree:true, childList:true, characterData:true, attributes:true});
        if (!initialize) scan();
    };
    if (document.documentElement) observe();
    else document.addEventListener('DOMContentLoaded', observe, {once:true});
    return true;
})
"""


def _evaluate(page: Page, expression: str) -> object:
    result = page.run_cdp("Runtime.evaluate", expression=expression, returnByValue=True)
    if not isinstance(result, dict) or result.get("exceptionDetails"):
        return None
    value = result.get("result")
    return value.get("value") if isinstance(value, dict) else None


class RenewalFeedback:
    """只接受当前提交触发的站点提示，浏览器弹窗继续正常显示。"""

    def __init__(self, page: Page) -> None:
        self.page = page
        self.marker = secrets.token_hex(16)
        self.active = False
        self.script_id = ""
        argument = json.dumps(self.marker)
        try:
            script = page.run_cdp(
                "Page.addScriptToEvaluateOnNewDocument",
                source=_OBSERVE + f"({argument}, false);",
            )
            self.script_id = script["identifier"]
            if _evaluate(page, _OBSERVE + f"({argument}, true);") is not True:
                raise ValueError("renewal_page_unverified")
        except Exception:
            self.close()
            raise ValueError("renewal_page_unverified") from None

    def activate(self) -> None:
        """在真实按钮点击前启用，不接收此前已有的成功提示。"""
        result = _evaluate(
            self.page,
            """(() => {
                if (location.origin !== 'https://hax.co.id') return false;
                const state = JSON.parse(sessionStorage.getItem('__hax_renewal_feedback'));
                if (!state || state.marker !== MARKER) return false;
                state.active = true;
                sessionStorage.setItem('__hax_renewal_feedback', JSON.stringify(state));
                return true;
            })()""".replace("MARKER", json.dumps(self.marker)),
        )
        if result is not True:
            raise ValueError("renewal_submission_unverified")
        self.active = True

    def read(self) -> dict[str, str]:
        """返回固定分类及 ISO 日期，不传递提示原文或网页内容。"""
        unknown = dict(status="unknown", date="", source="none", reason="none")
        if not self.active:
            return unknown
        try:
            value = _evaluate(
                self.page,
                "location.origin === 'https://hax.co.id' ? JSON.parse(sessionStorage.getItem('__hax_renewal_feedback')) : null",
            )
            if (
                not isinstance(value, dict)
                or set(value)
                != {"marker", "active", "status", "date", "source", "reason"}
                or value["marker"] != self.marker
                or value["active"] is not True
                or value["status"] not in {"success", "failed"}
                or value["source"] not in {"page", "dialog"}
                or value["reason"] not in {"none", "code", "captcha", "rejected"}
                or not isinstance(value["date"], str)
            ):
                return unknown
            expiry = (
                datetime.strptime(value["date"], "%B %d, %Y").date().isoformat()
                if value["status"] == "success" and value["reason"] == "none"
                else ""
            )
            if value["status"] == "success" and not expiry:
                return unknown
            return dict(
                status=value["status"],
                date=expiry,
                source=value["source"],
                reason=value["reason"],
            )
        except Exception:
            return unknown

    def close(self) -> None:
        """撤销当前观察器，不关闭页面、不注销站点会话。"""
        try:
            _evaluate(
                self.page,
                """(() => {
                    if (location.origin !== 'https://hax.co.id') return;
                    const current = window.__hax_renewal_feedback;
                    if (current && current.marker === MARKER) current.close();
                    const state = JSON.parse(sessionStorage.getItem('__hax_renewal_feedback'));
                    if (state && state.marker === MARKER) sessionStorage.removeItem('__hax_renewal_feedback');
                })()""".replace("MARKER", json.dumps(self.marker)),
            )
        except Exception:
            pass
        if self.script_id:
            try:
                self.page.run_cdp(
                    "Page.removeScriptToEvaluateOnNewDocument",
                    identifier=self.script_id,
                )
            except Exception:
                pass
            self.script_id = ""

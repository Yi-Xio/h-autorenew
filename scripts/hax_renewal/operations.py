"""浏览器中的 Hax 续期操作，副作用与结果核对分别执行。"""

import random
import time
from collections.abc import Callable
from typing import Any

from scripts.hax_result import RenewalFeedback
from scripts.web_sessions import HAX_ORIGIN

HAX_VPS_RENEW_URL = f"{HAX_ORIGIN}/vps-renew"
HAX_VPS_RENEW_CODE_URL = f"{HAX_ORIGIN}/vps-renew-code"


def open_renewal_form(browser: Any) -> None:
    """打开请求业务码的表单并等待原有页面初始化时间。"""
    browser.open(HAX_VPS_RENEW_URL)
    time.sleep(5)


def fill_site_address(browser: Any) -> None:
    """填写初始表单要求的站点地址。"""
    # 填 web_address（模拟真实用户输入）
    web_addr = "hax.co.id"
    print(f"  [INFO] 填写 web_address: '{web_addr}'")
    try:
        browser.execute_script("""
            var input = document.getElementById('web_address') || document.querySelector('input[name="web_address"]');
            if (input) {
                input.value = '';
                input.focus();
                input.dispatchEvent(new Event('focus', {bubbles: true}));
            }
        """)
        time.sleep(0.3)
        for ch in web_addr:
            browser.execute_script(f"""
                var input = document.getElementById('web_address') || document.querySelector('input[name="web_address"]');
                if (input) {{
                    input.value += '{ch}';
                    input.dispatchEvent(new Event('input', {{bubbles: true}}));
                    input.dispatchEvent(new Event('change', {{bubbles: true}}));
                }}
            """)
            time.sleep(random.uniform(0.05, 0.15))
        print("  [INFO] web_address 已填写")
    except Exception as e:
        print(f"  [WARN] web_address 填写失败: {e}")


def accept_agreement(browser: Any) -> None:
    """设置并读取协议选项。"""
    # 勾 agreement
    print("  [INFO] 勾选 agreement...")
    try:
        browser.execute_script("""
            var cb = document.querySelector('input[name="agreement"]');
            if (cb && !cb.checked) {
                cb.click();
                cb.dispatchEvent(new Event('change', {bubbles: true}));
            }
        """)
        time.sleep(0.5)
        checked = browser.execute_script(
            "return document.querySelector('input[name=\"agreement\"]') ? document.querySelector('input[name=\"agreement\"]').checked : false;"
        )
        print(f"  [INFO] agreement 状态: {'✅ 已勾选' if checked else '❌ 未勾选'}")
    except Exception as e:
        print(f"  [WARN] agreement 勾选失败: {e}")


def click_code_request(browser: Any) -> None:
    """执行初始表单的点击，由码页入口确认请求结果。"""
    print("  [INFO] 点击 Renew VPS...")
    try:
        browser.execute_script("""
            var btn = document.querySelector('button[name="submit_button"]');
            if (btn) btn.click();
        """)
        print("  [INFO] Renew VPS 已点击")
    except Exception as e:
        print(f"  [ERROR] 提交失败: {e}")
        raise


def wait_for_code_link(browser: Any) -> dict[str, str]:
    """在初始表单的响应中读取码页入口。"""
    print("  [INFO] 等待验证码页面入口...")
    deadline = time.time() + 30
    link_info = None
    while time.time() < deadline:
        try:
            link_info = browser.execute_script("""
                return (function() {
                    var resp = document.getElementById('response');
                    if (!resp || resp.innerHTML.length <= 10) return null;
                    var link = resp.querySelector('a[href*="renew-code"]');
                    if (!link) return null;
                    return {href: link.href || '', text: (link.textContent || '').trim()};
                })()
            """)
            if link_info:
                break
        except Exception:
            pass
        time.sleep(0.5)

    if not link_info:
        raise RuntimeError("30 秒内未找到 INPUT RENEW CODE 链接")
    return link_info


def open_code_page(browser: Any, link_info: dict[str, str]) -> str:
    """点击本轮码页入口，返回点击前的地址供后续检查。"""
    before_url = browser.get_current_url()
    print(f"  [INFO] 找到 INPUT RENEW CODE: {link_info.get('text', '')!r}")
    clicked = browser.execute_script("""
        return (function() {
            var resp = document.getElementById('response');
            var link = resp && resp.querySelector('a[href*="renew-code"]');
            if (!link) return false;
            link.click();
            return true;
        })()
    """)
    if not clicked:
        raise RuntimeError("INPUT RENEW CODE 链接点击失败")
    return before_url


def wait_for_code_page(browser: Any, before_url: str) -> None:
    """等待并核对码页已出现。"""
    deadline = time.time() + 20
    entered = False
    while time.time() < deadline:
        try:
            url = browser.get_current_url()
            entered = bool(
                "renew-code" in url.lower()
                or browser.execute_script("""
                    return !!(
                        document.querySelector('.col-sm-3 img') &&
                        document.querySelectorAll('.col-sm-3 img').length >= 2
                    );
                """)
            )
            if entered:
                print(f"  [INFO] INPUT RENEW CODE 点击成功，已进入验证码页: {url}")
                break
        except Exception:
            pass
        time.sleep(0.5)

    if not entered:
        raise RuntimeError(
            f"INPUT RENEW CODE 已点击，但 20 秒内未进入验证码页 "
            f"(before={before_url}, after={browser.get_current_url()})"
        )


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
    """把本轮业务码填入最终表单。"""
    # reCAPTCHA 解完后，输入续期验证码（TG bot 发来的）
    print("  [INFO] 输入续期验证码...")
    try:
        browser.execute_script(f"""
            var input = document.querySelector('input[name="code"], input[name="verification_code"], input[name="otp"]');
            if (input) {{
                input.value = '{code}';
                input.dispatchEvent(new Event('input', {{bubbles: true}}));
                input.dispatchEvent(new Event('change', {{bubbles: true}}));
            }}
        """)
        print("  [INFO] 验证码已输入")
    except Exception as e:
        print(f"  [ERROR] 验证码输入失败: {e}")
        raise

    time.sleep(1)


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


def click_final_button(browser: Any) -> None:
    """尝试点击最终按钮并在点击前启用本轮观察。"""
    clicked = False
    for locator in (
        'css:button[name="submit_button"]',
        'css:button[data-callback="onSubmit"]',
        "css:button.btn-primary",
        'css:button[type="submit"]',
        'css:input[type="submit"]',
    ):
        try:
            element = browser.page.ele(locator, timeout=2)
            if element:
                browser.renewal_submission.activate()
                element.click()
                clicked = True
                print(f"  [INFO] 真实元素点击成功: {locator}")
                break
        except Exception:
            continue
    if not clicked:
        print("  [ERROR] 所有按钮定位器都未命中，未发生任何点击")


def read_submission_feedback(browser: Any) -> tuple[dict[str, str], bool]:
    """读取本轮站点反馈和提交尝试状态，随后关闭观察器。"""
    tracker = getattr(browser, "renewal_submission", None)
    feedback = dict(status="unknown", date="", source="none", reason="none")
    attempted = bool(tracker and tracker.active)
    try:
        for attempt in range(6):
            if tracker:
                feedback = tracker.read()
            if feedback["status"] != "unknown" or not attempted:
                break
            if attempt < 5:
                time.sleep(4)
    finally:
        if tracker:
            tracker.close()
        browser.renewal_submission = None
    return feedback, attempted


def drain_result_alerts(browser: Any) -> str:
    """等待并点掉页面上所有 JS alert，返回最后一条弹窗文本。

    hax 提交续期后用 alert 弹结果（如 "Your VPS has been renewed until ..."），
    不处理会阻塞 DrissionPage 所有后续操作（"存在未处理的提示框"）。
    """
    texts = []
    page = browser.page
    for _ in range(30):  # 最多等 ~15 秒
        try:
            t = page.handle_alert(accept=True, timeout=0.5)
            if isinstance(t, str):
                texts.append(t)
                print(f"  [INFO] 已处理弹窗: {t[:120]}")
                time.sleep(0.5)
                continue
        except Exception:
            pass
        if texts:
            break
        time.sleep(0.5)
    return texts[-1] if texts else ""


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
    try:
        servers, error, screenshot = read_vps_info(browser, result.get("_idx", 0))
        if screenshot:
            result["screenshot"] = screenshot
            result["vps_info_screenshot"] = screenshot
        sid = str(result.get("server_id", ""))
        current = next((s for s in servers if str(s.get("id", "")) == sid), None)
        if current:
            new_valid = current.get("valid_until", "")
            if new_valid:
                result["info_valid_until"] = new_valid
                result["new_valid_until"] = new_valid
                result["expiry_info"] = (
                    f"{old_valid_until} → {new_valid}"
                    if old_valid_until
                    else f"新到期日: {new_valid}"
                )
                print(f"  [INFO] info 页面到期日对比: {old_valid_until} → {new_valid}")
                if (
                    not result.get("success")
                    and old_valid_until
                    and new_valid != old_valid_until
                ):
                    result["success"] = True
                    result["message"] = "续期成功（info 页面到期时间已更新）"
            result["status"] = current.get("status", result.get("status", ""))
            result["location"] = current.get("location", result.get("location", ""))
            result["hostname"] = current.get("hostname", result.get("hostname", ""))
        elif error:
            print(f"  [WARN] 最终 info 页面未找到服务器: {error}")
    except Exception as e:
        print(f"  [WARN] 最终跳转 info 页面失败: {type(e).__name__}: {e}")

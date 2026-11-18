"""按现有业务顺序执行单台 Hax VPS 的续期。"""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol

from scripts.hax_renewal import operations
from scripts.job_summary import REASON_LABELS
from scripts.public_report import WARP_FAILURE_REASONS, record_check
from scripts.web_sessions import AuthenticationError

CN_TZ = timezone(timedelta(hours=8))


class WarpError(RuntimeError):
    """WARP 失败只携带固定原因，不保留命令输出或出口信息。"""

    def __init__(self, reason: str) -> None:
        if reason not in WARP_FAILURE_REASONS:
            raise ValueError("invalid_warp_reason")
        self.reason = reason
        super().__init__(REASON_LABELS[reason])


class CaptchaBlocked(Exception):
    """验证码处理未完成，由调用方决定是否恢复。"""

    pass


class ArithmeticCaptchaUnavailable(CaptchaBlocked):
    """算术验证码无法识别或填入。"""

    pass


class WarpCounters(Protocol):
    """续期流程读取的 WARP 统计。"""

    current_tag: str
    rotation_count: int
    warp_refresh_count: int


@dataclass(frozen=True)
class RenewalServices:
    """由 Hax 入口提供的浏览器准备、网络和信息页操作。"""

    prepare_initial_page: Callable[[Any], None]
    check_initial_form: Callable[[Any, int], None]
    ensure_warp_ready: Callable[[], None]
    restart_warp: Callable[[], bool]
    handoff_to_warp: Callable[[Any, str | None], bool]
    prepare_warp_page: Callable[[Any, int, str, str | None], None]
    prepare_final_form: Callable[[Any, int, str, str | None], bool]
    refresh_code_page: Callable[[Any, int, str, str | None], bool]
    clean_page: Callable[[Any], None]
    capture: Callable[[Any, int, str], None]
    read_vps_info: Callable[[Any, int], tuple[list[dict[str, str]], str, str | None]]
    warp_counters: Callable[[], WarpCounters]


def cn_now() -> datetime:
    return datetime.now(CN_TZ)


def _parse_iso_date(s: str) -> date | None:
    """解析 "YYYY-MM-DD" 为 date，失败返回 None。"""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None


def safe_sid_for_filename(sid: str) -> str:
    """遮盖续期日志中的 VPS 标识。"""
    s = str(sid)
    if len(s) <= 3:
        return s
    return f"{s[0]}{'*' * (len(s) - 3)}{s[-2:]}"


def collect_result(
    browser: Any,
    result: dict[str, Any],
    old_valid_until: str,
    read_vps_info: Callable[[Any, int], tuple[list[dict[str, str]], str, str | None]],
) -> bool:
    """以本轮真实提交后的站点成功提示为主，另行核对信息页日期。"""
    # Hax 用原生 alert 显示结果；先处理它，避免阻塞后续页面操作。
    # 观察器在最终按钮点击前启用，只保留本轮提示的分类和日期。
    try:
        operations.drain_result_alerts(browser)
    except Exception as e:
        print(f"  [WARN] 处理弹窗异常: {type(e).__name__}: {e}")
    feedback, attempted = operations.read_submission_feedback(browser)

    result["feedback_status"] = feedback["status"]
    result["submission_attempted"] = attempted
    confirmed = feedback["status"] == "success"
    if confirmed:
        new_valid = feedback["date"]
        result["banner_new_valid_until"] = new_valid
        result["new_valid_until"] = new_valid
        result["expiry_info"] = (
            f"{old_valid_until} → {new_valid}"
            if old_valid_until
            else f"新到期日: {new_valid}"
        )
        result["message"] = "续期成功"
        print("  [INFO] 已核实本轮站点续期成功提示")
    else:
        messages = {
            "code": "续期失败: 验证码错误或已失效",
            "captcha": "续期失败: captcha 被服务器拒绝",
            "rejected": "续期失败: 网站拒绝本次操作",
            "none": "续期结果待核实" if attempted else "未确认最终按钮提交",
        }
        result["message"] = messages[feedback["reason"]]
        if feedback["reason"] == "captcha":
            record_check("captcha", "site_feedback", "rejected")
        result["detailMessage"] = ""
        result["public_reason"] = (
            "renewal_unverified"
            if attempted and feedback["status"] == "unknown"
            else "captcha_failed"
            if feedback["reason"] == "captcha"
            else "renewal_failed"
        )
        print(f"  [WARN] {result['message']}")

    # 最后回到 VPS info 页面，截图和到期日仍以该页为准。
    # 同一天重复续期可显示成功而日期不变，不把这种情况描述为已增加天数。
    result["success"] = confirmed
    message = result["message"]
    operations.capture_final_info(browser, result, old_valid_until, read_vps_info)
    result["message"] = message
    result["success"] = confirmed
    return confirmed


def _try_final_submission(
    browser: Any,
    code: str,
    idx: int,
    sid_f: str,
    proxy: str | None,
    services: RenewalServices,
) -> None:
    """准备最终表单并发起提交，结果由调用方读取。"""
    if not services.prepare_final_form(browser, idx, sid_f, proxy):
        return

    operations.fill_renewal_code(browser, code)
    operations.position_final_form(browser)
    services.capture(browser, idx, f"renew_{sid_f}_complete_code")

    # 只用真实鼠标点击（用户实证：之前跑出进度条的成功轮次就是真实点击）。
    # 不做任何 JS click / XHR POST / form.submit 兜底——那些反而绕过了
    # 网站对「真人交互」的判定，导致 POST 被服务器无视。
    try:
        # 点击前：滚动按钮到视口中央 + 清理广告浮层（含 24h 看广告弹窗）
        services.clean_page(browser)
        operations.align_final_button(browser)
        operations.observe_submission(browser)
        operations.click_final_button(browser)
    except Exception as e:
        print(f"  [ERROR] 续期的最终按钮点击出错: {e}")

    # 点击后 3 秒截提交结果页，再由外层继续等待并跳转 info。
    time.sleep(3)
    services.capture(browser, idx, f"renew_{sid_f}_after_final_submit")


def _submit_and_check(
    browser: Any,
    code: str,
    idx: int,
    sid_f: str,
    old_valid_until: str,
    proxy: str | None,
    result: dict[str, Any],
    services: RenewalServices,
    max_warp_retries: int,
) -> None:
    """按现有条件提交并读取结果，保留站点明确拒绝时的恢复分支。"""
    for code_attempt in range(1, max_warp_retries + 1):
        # 5a. 提交验证码页面（算术 + reCAPTCHA + 最终提交）
        _try_final_submission(browser, code, idx, sid_f, proxy, services)

        # 5b. 检查续期结果
        if collect_result(browser, result, old_valid_until, services.read_vps_info):
            break  # 成功，退出

        # captcha 错误时，刷新 vps-renew-code 页面重试
        resp_msg = result.get("message", "")
        if "captcha" in resp_msg.lower() and code_attempt < max_warp_retries:
            print(
                f"  [INFO] captcha 错误，换 WARP IP 后重试 ({code_attempt}/{max_warp_retries})..."
            )
            if services.refresh_code_page(browser, idx, sid_f, proxy):
                continue  # 新 IP + 新验证码页面后重试本轮最终提交
            result["public_reason"] = "warp_failed"
            result["message"] = "续期失败: captcha 被服务器拒绝，WARP 换 IP 失败"
            result["detailMessage"] = "captcha 被服务器拒绝，WARP 换 IP 失败"
            break

        break  # 非 captcha 错误或重试耗尽，退出内层循环


def run_renewal(
    browser: Any,
    server_info: dict[str, str],
    idx: int,
    phone_mask: str,
    proxy: str | None,
    *,
    services: RenewalServices,
    min_valid_days: int,
    max_renew_retries: int,
    max_warp_retries: int,
) -> dict[str, Any]:
    """按当前业务规则续期一台 VPS，返回供账号入口汇总和通知的结果。"""
    sid = server_info.get("id", "")
    server_name = server_info.get("name", "Unknown")
    hostname = server_info.get("hostname", "")
    old_valid_until = server_info.get("valid_until", "")

    # ── 到期日充足判断：剩余天数 ≥ min_valid_days 时跳过续期 ──────────
    valid_date = _parse_iso_date(old_valid_until)
    base_date = _parse_iso_date(server_info.get("current_date", "")) or cn_now().date()
    if valid_date is not None and (valid_date - base_date).days >= min_valid_days:
        days_left = (valid_date - base_date).days
        result = {
            "server_id": sid,
            "server_name": server_name,
            "hostname": hostname,
            "status": server_info.get("status", ""),
            "ipv6": server_info.get("ipv6", ""),
            "location": server_info.get("location", ""),
            "phoneMask": phone_mask,
            "success": True,
            "skipped": True,
            "days_left": days_left,
            "message": f"✅ 有效期充足（剩余 {days_left} 天，至 {old_valid_until}），无需续期",
            "detailMessage": "",
            "screenshot": None,
            "warp_swaps": 0,
            "warp_refresh": 0,
            "retry_count": 0,
            "old_valid_until": old_valid_until,
            "new_valid_until": old_valid_until,
            "vps_info_screenshot": server_info.get("vps_info_screenshot", ""),
            "_idx": idx,
        }
        print(f"\n{'─' * 40}")
        print(
            f"  [INFO] ✅ 跳过续期: {server_name} 剩余 {days_left} 天（≥ {min_valid_days} 天），无需续期"
        )
        print(f"{'─' * 40}")
        return result

    # 记录本次续期过程中的换 IP 次数、warp 刷新次数与外层重试次数
    warp = services.warp_counters()
    warp.current_tag = phone_mask
    warp_start = warp.rotation_count
    warp_refresh_start = warp.warp_refresh_count

    result = {
        "server_id": sid,
        "server_name": server_name,
        "hostname": hostname,
        "status": server_info.get("status", ""),
        "ipv6": server_info.get("ipv6", ""),
        "location": server_info.get("location", ""),
        "phoneMask": phone_mask,
        "success": False,
        "message": "",
        "detailMessage": "",
        "screenshot": None,
        "warp_swaps": 0,
        "warp_refresh": 0,
        "retry_count": 0,
        "old_valid_until": old_valid_until,
        "new_valid_until": "",
        "vps_info_screenshot": server_info.get("vps_info_screenshot", ""),
        "_idx": idx,
    }

    sid_f = re.sub(r"[^\w]", "_", sid)

    print(f"\n{'─' * 40}")
    print(f"  [INFO] 续期: {server_name} (id={safe_sid_for_filename(sid)})")
    print(f"{'─' * 40}")

    # 算术验证码失败时额外保留一次换 IP 重试机会。
    max_attempts = max_renew_retries + 1
    forced_arithmetic_retry_used = False
    for renew_attempt in range(1, max_attempts + 1):
        # 重试时清除上一轮的截图和日期，避免把旧页面当作本次最终结果。
        result["screenshot"] = None
        result["vps_info_screenshot"] = ""
        result["info_valid_until"] = ""
        result["feedback_status"] = "unknown"
        result["submission_attempted"] = False
        if renew_attempt > 1:
            print(f"  [INFO] 续期重试 {renew_attempt}/{max_renew_retries}...")

        result["public_reason"] = "renewal_failed"
        try:
            # 提交之前记录业务消息基线；不使用历史旧码或操作 Telegram 按钮。
            browser.telegram_reader.arm()
            operations.open_renewal_form(browser)
            services.prepare_initial_page(browser)
            operations.fill_site_address(browser)
            operations.accept_agreement(browser)
            services.check_initial_form(browser, idx)
            # 提交（点击 Renew VPS 按钮）
            services.capture(browser, idx, f"renew_{sid_f}_before_submit")
            operations.click_code_request(browser)
            time.sleep(5)
            services.capture(browser, idx, f"renew_{sid_f}_after_submit")

            # 进入码页，站点验证在浏览器交接后处理。
            link_info = operations.wait_for_code_link(browser)
            before_url = operations.open_code_page(browser, link_info)
            operations.wait_for_code_page(browser, before_url)
            operations.position_code_form(browser)

            # 4. 最终续期页面强制脱离 sing-box，改由系统 WARP 直连。
            # 入口点击可通过代理完成，但验证码和最后提交必须使用同一 WARP 出口。
            print("  [INFO] 已进入最终续期页面，切换为 WARP 直连浏览器...")
            if renew_attempt == 1:
                services.ensure_warp_ready()
            else:
                services.restart_warp()
            if not services.handoff_to_warp(browser, operations.HAX_VPS_RENEW_CODE_URL):
                result["public_reason"] = "warp_failed"
                raise CaptchaBlocked("最终续期页面 WARP 直连浏览器重建失败")

            services.prepare_warp_page(browser, idx, sid_f, proxy)

            # 5. 获取 TG 验证码
            result["public_reason"] = "renewal_code_unavailable"
            # 从固定业务会话等待本轮新码，不输出验证码。
            code = browser.telegram_reader.wait_for_code(timeout=150)
            result["public_reason"] = "renewal_failed"

            _submit_and_check(
                browser,
                code,
                idx,
                sid_f,
                old_valid_until,
                proxy,
                result,
                services,
                max_warp_retries,
            )

            if result["success"]:
                break  # 成功，退出外层重试

            if renew_attempt < max_renew_retries:
                print("  [INFO] 等待 5s 后重试...")
                time.sleep(5)
                continue
            break

        except WarpError as e:
            result["public_reason"] = e.reason
            result["message"] = str(e)
            break
        except AuthenticationError as e:
            result["public_reason"] = e.reason
            result["message"] = REASON_LABELS[e.reason]
            break
        except CaptchaBlocked as e:
            # 保留验证码失败的具体环节，不由异常类型推断出口被封锁。
            # 此处保留已有恢复调用；验证码失败类型与换出口条件由后续重试改造统一。
            # 算术验证码即使到达普通重试上限，也额外保留一次 WARP 重试。
            is_arithmetic_failure = isinstance(e, ArithmeticCaptchaUnavailable)
            can_retry = renew_attempt < max_renew_retries
            if is_arithmetic_failure and not forced_arithmetic_retry_used:
                can_retry = True
                forced_arithmetic_retry_used = True
            if can_retry:
                print(f"  [WARN] {type(e).__name__}: {e}")
                print("  [INFO] 验证码失败，立即切换 WARP IP 后重试...")
                try:
                    if services.restart_warp() and services.handoff_to_warp(
                        browser, None
                    ):
                        time.sleep(5)
                        continue
                except WarpError as warp_error:
                    result["public_reason"] = warp_error.reason
                    result["message"] = str(warp_error)
                    break
            if result.get("public_reason") != "warp_failed":
                result["public_reason"] = "captcha_failed"
            result["message"] = f"验证码失败: {e}"
            break
        except Exception as e:
            print(f"  [ERROR] 续期异常: {e}")
            if renew_attempt < max_renew_retries:
                print("  [INFO] 等待 5s 后重试...")
                time.sleep(5)
                continue
            result["message"] = f"续期异常: {e}"
            break

    result["warp_swaps"] = warp.rotation_count - warp_start
    result["warp_refresh"] = warp.warp_refresh_count - warp_refresh_start
    result["retry_count"] = max(renew_attempt - 1, 0)
    print(
        f"  [STATS][{result.get('phoneMask', '')}] 续期重试={result['retry_count']} ip更换={result['warp_swaps']} warp刷新={result['warp_refresh']}"
    )

    print(f"  {'[INFO]' if result['success'] else '[ERROR]'} {result['message']}")
    return result

"""按现有业务顺序执行单台 Hax VPS 的续期。"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol

from scripts.hax_renewal import operations
from scripts.hax_renewal.retry_report import (
    RetryAttempt,
    RetryStep,
    ValidationPage,
    retry_count,
    start_retry,
    validation_failure_detail,
)
from scripts.hax_renewal.timing import require_time
from scripts.job_summary import REASON_LABELS
from scripts.public_report import WARP_FAILURE_REASONS, latest_check, record_check
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
    handoff_to_warp: Callable[[Any, str | None], bool]
    prepare_warp_page: Callable[[Any, int, str, str | None], None]
    prepare_final_form: Callable[[Any, int, str, str | None], bool]
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
    # 观察器在最终按钮点击前启用，只保留本轮提示的分类和日期。
    feedback, tracked = operations.read_submission_feedback(browser)
    attempted = bool(result.get("submission_attempted") or tracked)
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
        result["detailMessage"] = (
            validation_failure_detail(
                ValidationPage.CODE_FORM, {"step": "site_feedback", "result": "rejected"}
            )
            if feedback["reason"] == "captcha"
            else ""
        )
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


def _prepare_initial_form(browser: Any, services: RenewalServices) -> None:
    """为请求续期码准备普通表单，页面检查与字段操作共用一次重开。"""
    for attempt in range(2):
        require_time()
        retry = start_retry(RetryStep.INITIAL_FORM) if attempt else None
        try:
            operations.open_renewal_form(browser)
            services.prepare_initial_page(browser)
            operations.fill_site_address(browser)
            operations.accept_agreement(browser)
            if retry:
                retry.recovered = True
            return
        except (CaptchaBlocked, TimeoutError):
            raise
        except AuthenticationError as error:
            if error.reason != "hax_page_unverified":
                raise
        except Exception:
            pass
        current = browser.get_current_url()
        if operations.is_hax_url(current, "/login"):
            raise AuthenticationError("hax_session_invalid")
        if attempt or not operations.is_hax_url(current):
            raise AuthenticationError("hax_page_unverified")
    raise AuthenticationError("hax_page_unverified")


def _request_code(browser: Any, result: dict[str, Any]) -> RetryAttempt | None:
    """请求可能已发出时，后续只处理本次响应。"""
    for attempt in range(2):
        require_time()
        # 记录业务消息基线，不使用历史消息或操作 Telegram 按钮。
        browser.telegram_reader.arm()
        require_time()
        retry = start_retry(RetryStep.CODE_REQUEST) if attempt else None
        result["code_request_pending"] = True
        try:
            attempted = operations.click_code_request(browser)
        except Exception:
            # 浏览器可能在点击实际执行以后丢失响应。
            return retry
        if attempted:
            return retry
        result["code_request_pending"] = False
    raise AuthenticationError("renewal_code_unavailable")


def _open_code_page(browser: Any, link: str) -> None:
    """只从本轮已核对的入口重开码页，保留续期码请求。"""
    for attempt in range(2):
        require_time()
        retry = start_retry(RetryStep.CODE_PAGE) if attempt else None
        try:
            operations.open_code_page(browser, link)
        except Exception:
            pass
        if operations.wait_for_code_page(browser):
            if retry:
                retry.recovered = True
            return
        require_time()
        current = browser.get_current_url()
        if attempt or not (
            operations.can_reopen_hax_page(current, link)
            or operations.is_hax_url(current, "/vps-renew")
        ):
            break
    raise AuthenticationError("hax_page_unverified")


def _check_site_validation(
    result: dict[str, Any],
    page: ValidationPage,
    check: Callable[[], bool | None],
) -> bool | None:
    """让本次失败只使用当前验证步骤产生的诊断。"""
    previous = latest_check("captcha")
    try:
        return check()
    except CaptchaBlocked:
        current = latest_check("captcha")
        result["detailMessage"] = validation_failure_detail(
            page, current if current is not previous else None
        )
        raise


def _try_final_submission(
    browser: Any,
    code: str,
    idx: int,
    sid_f: str,
    proxy: str | None,
    services: RenewalServices,
    result: dict[str, Any],
) -> None:
    """完成表单准备，只有尚未尝试点击时才能恢复按钮操作。"""
    require_time()
    if not _check_site_validation(
        result,
        ValidationPage.CODE_FORM,
        lambda: services.prepare_final_form(browser, idx, sid_f, proxy),
    ):
        raise AuthenticationError("hax_page_unverified")
    operations.fill_renewal_code(browser, code)
    operations.position_final_form(browser)
    services.capture(browser, idx, f"renew_{sid_f}_complete_code")

    # 滚动按钮到视口中央并清理广告浮层，随后启用反馈观察。
    services.clean_page(browser)
    for attempt in range(2):
        require_time()
        retry = start_retry(RetryStep.FINAL_BUTTON) if attempt else None
        try:
            operations.align_final_button(browser)
            operations.observe_submission(browser)
            if operations.click_final_button(browser):
                result["submission_attempted"] = True
                if retry:
                    retry.recovered = True
                return
        except (AuthenticationError, TimeoutError):
            raise
        except Exception:
            pass
        finally:
            if not result["submission_attempted"]:
                operations.close_submission(browser)
    raise AuthenticationError("hax_page_unverified")


def run_renewal(
    browser: Any,
    server_info: dict[str, str],
    idx: int,
    phone_mask: str,
    proxy: str | None,
    *,
    services: RenewalServices,
    min_valid_days: int | None,
) -> dict[str, Any]:
    """按当前业务规则续期一台 VPS，返回供账号入口汇总和通知的结果。"""
    sid = server_info.get("id", "")
    server_name = server_info.get("name", "Unknown")
    hostname = server_info.get("hostname", "")
    old_valid_until = server_info.get("valid_until", "")

    # ── 到期日充足判断：剩余天数 ≥ min_valid_days 时跳过续期 ──────────
    valid_date = _parse_iso_date(old_valid_until)
    base_date = _parse_iso_date(server_info.get("current_date", "")) or cn_now().date()
    if (
        min_valid_days is not None
        and valid_date is not None
        and (valid_date - base_date).days >= min_valid_days
    ):
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

    # 记录本次续期过程中的 WARP 统计和局部恢复次数。
    prior_retries = retry_count()
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
        "submission_attempted": False,
        "feedback_status": "unknown",
        "code_request_pending": False,
        "message": "",
        "detailMessage": "",
        "screenshot": None,
        "warp_swaps": 0,
        "warp_refresh": 0,
        "retry_count": 0,
        "old_valid_until": old_valid_until,
        "new_valid_until": "",
        "vps_info_screenshot": "",
        "_idx": idx,
    }

    if valid_date is None:
        result["public_reason"] = "vps_read_failed"
        result["message"] = "未能读取有效的 VPS 到期日"
        return result

    sid_f = re.sub(r"[^\w]", "_", sid)

    print(f"\n{'─' * 40}")
    print(f"  [INFO] 续期: {server_name} (id={safe_sid_for_filename(sid)})")
    print(f"{'─' * 40}")

    try:
        _prepare_initial_form(browser, services)
        _check_site_validation(
            result,
            ValidationPage.INITIAL_FORM,
            lambda: services.check_initial_form(browser, idx),
        )
        services.capture(browser, idx, f"renew_{sid_f}_before_submit")
        code_retry = _request_code(browser, result)
        link = operations.wait_for_code_link(browser)
        if code_retry:
            code_retry.recovered = True
        _open_code_page(browser, link)
        operations.position_code_form(browser)

        # 初始请求可经代理完成，后续码页仍使用原有的 WARP 连接。
        require_time()
        try:
            services.ensure_warp_ready()
        except WarpError as error:
            if error.reason != "warp_ip_query_failed":
                raise
            require_time()
            retry = start_retry(RetryStep.WARP_CHECK)
            services.ensure_warp_ready()
            retry.recovered = True
        require_time()
        if not services.handoff_to_warp(browser, link):
            raise WarpError("warp_failed")
        _check_site_validation(
            result,
            ValidationPage.CODE_FORM,
            lambda: services.prepare_warp_page(browser, idx, sid_f, proxy),
        )

        # 业务码基线随本轮请求保留，WARP 浏览器重建不会重置它。
        require_time()
        code = browser.telegram_reader.wait_for_code(timeout=150)
        result["code_request_pending"] = False
        _try_final_submission(browser, code, idx, sid_f, proxy, services, result)
        collect_result(browser, result, old_valid_until, services.read_vps_info)
    except Exception as error:
        if result["success"]:
            # 保存已核实的业务结果；信息页和截图异常独立收尾。
            print("  [WARN] 未能补充 VPS 信息")
        elif result["submission_attempted"]:
            result["public_reason"] = "renewal_unverified"
        elif isinstance(error, (AuthenticationError, WarpError)):
            result["public_reason"] = error.reason
        elif isinstance(error, CaptchaBlocked):
            result["public_reason"] = "captcha_failed"
        elif isinstance(error, TimeoutError):
            result["public_reason"] = "timeout"
        else:
            result["public_reason"] = "renewal_failed"
        if not result["success"]:
            result["message"] = REASON_LABELS[result["public_reason"]]
            print(f"  [WARN] 续期操作中止: {type(error).__name__}")

    result["retry_count"] = retry_count() - prior_retries
    result["warp_swaps"] = warp.rotation_count - warp_start
    result["warp_refresh"] = warp.warp_refresh_count - warp_refresh_start
    print(
        f"  [STATS][{result.get('phoneMask', '')}] 续期重试={result['retry_count']} ip更换={result['warp_swaps']} warp刷新={result['warp_refresh']}"
    )

    print(f"  {'[INFO]' if result['success'] else '[ERROR]'} {result['message']}")
    return result

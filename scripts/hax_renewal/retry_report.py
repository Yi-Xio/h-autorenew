"""记录 Hax 局部恢复操作，供本次 Telegram 文字报告使用。"""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from itertools import groupby


class RetryStep(StrEnum):
    TELEGRAM_READ = "重新读取 Telegram Web"
    TELEGRAM_CLIENT = "重新加载 Telegram Web"
    TELEGRAM_CHAT = "重新打开 Telegram 中的 Hax 业务会话"
    HAX_LOGIN = "重新打开 Hax 登录页"
    HAX_AUTH_READ = "重新读取 Hax 的 Telegram 授权组件"
    HAX_AUTH_WIDGET = "重新打开 Hax 的 Telegram 授权组件"
    VPS_INFO = "重新读取 VPS 信息页"
    INITIAL_FORM = "重新打开初始续期表单"
    SITE_ADDRESS = "重新检查站点地址字段"
    AGREEMENT = "重新核对协议勾选状态"
    CODE_REQUEST = "重新尝试请求续期码"
    CODE_PAGE = "重新打开本轮续期码页面"
    WARP_CHECK = "重新核实 WARP 连接"
    WARP_CONNECT = "重新建立 WARP 连接"
    WARP_COOKIES = "重新读取 Hax 会话"
    WARP_BROWSER = "重新启动 WARP 浏览器"
    WARP_CODE_PAGE = "在 WARP 浏览器中重新打开续期码页面"
    RENEWAL_CODE = "重新检查续期码字段"
    FINAL_BUTTON = "重新准备最终按钮与反馈观察"
    FINAL_INFO = "重新读取续期后的到期日"


class ValidationPage(StrEnum):
    INITIAL_FORM = "初始续期表单"
    CODE_FORM = "最终续期码表单"


_VALIDATION_DETAILS = {
    ("turnstile", "token_missing"): "站点验证未完成",
    ("recaptcha", "anchor_missing"): "等待结束后仍未找到 reCAPTCHA 控件",
    ("recaptcha", "anchor_loading"): "已找到 reCAPTCHA 容器，控件仍未就绪",
    ("recaptcha", "frame_unavailable"): "未能读取 reCAPTCHA 容器",
    ("recaptcha", "inspection_failed"): "未能核实 reCAPTCHA 控件状态",
    ("recaptcha", "blocked"): "站点当前限制验证",
    ("site_feedback", "rejected"): "站点拒绝本轮提交的验证结果",
}


def validation_failure_detail(
    page: ValidationPage, check: Mapping[str, str] | None = None
) -> str:
    """只展示续期阶段和经过确认的固定诊断，不使用异常原文。"""
    details = check or {}
    status = _VALIDATION_DETAILS.get(
        (details.get("step", ""), details.get("result", "")), "站点验证未完成"
    )
    return f"{page.value}，{status}"


@dataclass
class RetryAttempt:
    """一次实际开始的恢复操作，完成核对后才标记为已恢复。"""

    step: RetryStep
    recovered: bool = False
    observation_seconds: int | None = None


_attempts: ContextVar[list[RetryAttempt] | None] = ContextVar(
    "hax_retry_attempts", default=None
)


@contextmanager
def retry_scope(*, inherit: bool = False) -> Iterator[None]:
    """账号从空记录开始，单台 VPS 只继承本账号的准备记录。"""
    prior = _attempts.get() if inherit else None
    token = _attempts.set(list(prior or []))
    try:
        yield
    finally:
        _attempts.reset(token)


def start_retry(step: RetryStep) -> RetryAttempt:
    """在确实要执行恢复操作时登记。"""
    attempt = RetryAttempt(step)
    attempts = _attempts.get()
    if attempts is not None:
        attempts.append(attempt)
    return attempt


def retry_count() -> int:
    """统计当前报告范围内实际开始的重试。"""
    return len(_attempts.get() or ())


def format_retry_report() -> str:
    """只渲染固定的操作名称和恢复结果。"""
    attempts = _attempts.get()
    if attempts is None:
        return ""
    lines = ["", f"🔁 重试操作（{len(attempts)} 次）"]
    for step, group in groupby(attempts, key=lambda item: item.step):
        items = list(group)
        outcomes = [item.recovered for item in items]
        if len(outcomes) == 1:
            result = "已恢复" if outcomes[0] else "未恢复"
        else:
            recovered = sum(outcomes)
            result = (
                f"{len(outcomes)} 次，已恢复 {recovered} 次，"
                f"未恢复 {len(outcomes) - recovered} 次"
            )
        rendered = f"• {step.value}（{result}）"
        observations = [
            str(item.observation_seconds) for item in items if item.observation_seconds is not None
        ]
        if observations:
            rendered += f"，重开后观察 {'、'.join(observations)} 秒"
        lines.append(rendered)
    return "\n".join(lines) + "\n"

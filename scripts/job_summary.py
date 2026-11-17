"""以固定字段生成中文摘要，原始凭据、页面及异常不参与渲染。"""

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from scripts.public_report import (
    DETAIL_COUNTERS,
    REASONS,
    STAGES,
    validate_details,
    validate_snapshot,
)

RUNTIME_FILE = Path("output/renewal-result.json")
RUNTIME_REASONS = REASONS | {
    "timeout",
    "cancelled",
    "worker_error",
    "invalid_configuration_or_report",
}
STAGE_LABELS = {
    "preparing": "准备运行",
    "login": "登录网站",
    "reading_vps": "读取 VPS 信息",
    "renewing": "执行续期",
    "notifying": "发送通知",
    "complete": "结果汇总",
}
REASON_LABELS = {
    "none": "",
    "login_failed": "网站登录失败",
    "vps_read_failed": "未能读取 VPS 信息",
    "renewal_failed": "续期失败",
    "renewal_unverified": "尚未确认到期日延长",
    "captcha_failed": "验证码处理失败",
    "renewal_code_unavailable": "未获取到续期验证码",
    "warp_failed": "WARP 切换失败",
    "warp_registration_failed": "WARP 注册失败",
    "warp_connection_failed": "未能核实 WARP 连接",
    "warp_ip_query_failed": "未取得有效的 WARP 出口 IP",
    "warp_new_exit_unavailable": "未取得新的 WARP 出口",
    "timeout": "续期脚本运行超时",
    "cancelled": "执行已取消",
    "worker_error": "续期进程异常退出",
    "invalid_configuration_or_report": "配置或运行报告未通过校验",
}


def empty_details() -> dict[str, Any]:
    """初始计数不代表通知已经执行。"""
    return {**dict.fromkeys(DETAIL_COUNTERS, 0), "reason": "none"}


def validate_runtime(value: object) -> dict[str, Any]:
    """公开快照只接受受控枚举及数字，额外字段一律拒绝。"""
    keys = {
        "version",
        "platform",
        "index",
        "result",
        "stage",
        "reason",
        "elapsed_seconds",
        "summary",
        "details",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("invalid_runtime")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("invalid_runtime")
    for key, allowed in (
        ("platform", {"hax", "woiden"}),
        ("result", {"complete", "failed", "cancelled"}),
        ("stage", STAGES),
        ("reason", RUNTIME_REASONS),
    ):
        if not isinstance(value[key], str) or value[key] not in allowed:
            raise ValueError("invalid_runtime")
    if type(value["index"]) is not int or not 0 <= value["index"] <= 256:
        raise ValueError("invalid_runtime")
    if (
        type(value["elapsed_seconds"]) is not int
        or not 0 <= value["elapsed_seconds"] <= 86400
    ):
        raise ValueError("invalid_runtime")
    validate_details(value["details"])
    if value["summary"] is not None:
        validate_snapshot(
            {
                "version": 2,
                "stage": "complete",
                "summary": value["summary"],
                "details": value["details"],
            }
        )
    return dict(value)


def save_runtime(root: Path, value: object) -> None:
    """公开快照不含身份标识和页面材料，也不会上传为 Artifact。"""
    clean = validate_runtime(value)
    path = root / RUNTIME_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(clean, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def render_summary(
    platform: str, index: int, environ: Mapping[str, str], root: Path
) -> str:
    """生成单账号摘要，只读取受控快照和固定步骤结果。"""
    if (
        platform not in {"hax", "woiden"}
        or type(index) is not int
        or not 0 <= index <= 256
    ):
        raise ValueError("invalid_identity")
    runtime = None
    invalid = False
    try:
        if environ.get("RENEW_OUTCOME") not in {"success", "failure", "cancelled"}:
            raise FileNotFoundError
        path = root / RUNTIME_FILE
        if path.stat().st_size > 16384:
            raise ValueError("invalid_runtime")
        runtime = validate_runtime(json.loads(path.read_text(encoding="utf-8")))
        if runtime["platform"] != platform or runtime["index"] != index:
            raise ValueError("invalid_identity")
    except FileNotFoundError:
        pass
    except (ValueError, OSError, UnicodeError):
        runtime, invalid = None, True
    skipped = (
        environ.get("SHOULD_RUN") == "false"
        and environ.get("CHECK_OUTCOME") == "success"
    )
    summary = runtime["summary"] if runtime else None
    details = runtime["details"] if runtime else empty_details()
    reason = runtime["reason"] if runtime else "none"
    stage = STAGE_LABELS[runtime["stage"]] if runtime else "未启动续期脚本"
    headline, business = "⚠️ 未取得可靠结果", "未完成"
    if skipped:
        headline, business = "⏭️ 无需续期", "缓存预筛通过，未启动续期脚本"
        days = _number(environ.get("DAYS_LEFT", ""), 4_000_000)
        if days is not None:
            business += f"；记录显示剩余 {days} 天"
        stage = "缓存预筛"
    elif summary:
        parts = []
        for key, label in (
            ("renewed", "已延长"),
            ("skipped", "无需续期"),
            ("failed", "续期失败"),
            ("unverified", "待核实"),
        ):
            count = (
                summary[key] - details["account_failures"]
                if key == "failed"
                else summary[key]
            )
            if count:
                parts.append(f"{label} {count} 台")
        if details["account_failures"]:
            parts.append(f"账号检查失败 {details['account_failures']} 个")
        if summary["no_vps"]:
            parts.append(f"无可用 VPS {summary['no_vps']} 个账号")
        business = "；".join(parts)
        if summary["failed"]:
            headline = (
                "❌ 部分失败"
                if summary["renewed"] or summary["skipped"]
                else "❌ 执行失败"
            )
        elif summary["unverified"]:
            headline = "⚠️ 续期结果待核实"
        elif summary["renewed"]:
            headline = "✅ 已验证延长"
        elif summary["skipped"]:
            headline = "⏭️ 无需续期"
        else:
            headline = "📭 无可续期 VPS"
    if runtime and runtime["result"] == "cancelled":
        headline = "⏹️ 执行已取消"
    elif (
        runtime
        and runtime["result"] == "failed"
        and not (summary and (summary["failed"] or summary["unverified"]))
    ):
        headline = "❌ 执行异常"
    if not runtime and not skipped:
        failures = (
            ("CHECKOUT_OUTCOME", "检出仓库", "仓库检出失败"),
            ("CACHE_KEY_OUTCOME", "准备账号配置", "账号配置检查失败"),
            ("CHECK_OUTCOME", "缓存预筛", "预筛检查失败"),
            ("PYTHON_OUTCOME", "准备 Python", "Python 环境准备失败"),
            ("DEPENDENCIES_OUTCOME", "安装运行依赖", "运行依赖安装失败"),
            ("WARP_OUTCOME", "连接 WARP", "WARP 连接失败"),
            ("PROXY_OUTCOME", "准备代理", "代理准备失败"),
        )
        for key, label, message in failures:
            if environ.get(key) == "failure":
                headline, stage = "❌ 启动失败", label
                reason = "none"
                business = message
                break
        if invalid:
            business = "运行报告未通过校验"
        elif environ.get("RENEW_OUTCOME") == "failure" and business == "未完成":
            business = "续期进程未提供可靠报告"
        if environ.get("JOB_CANCELLED") == "true":
            headline, business = "⏹️ 执行已取消", "未取得完整结果"
    text = _notification(details, "text", "条")
    photo = _notification(details, "photo", "张")
    if skipped:
        text = {"sent": "已发送 1 条", "failed": "发送失败 1 条"}.get(
            environ.get("NOTIFY_STATUS", ""), "未执行"
        )
        if text == "未执行" and environ.get("NOTIFY_OUTCOME") == "failure":
            text = "通知步骤失败"
        photo = "本轮未生成截图"
    elif (
        details["photo_sent"] + details["photo_failed"] == 0
        and details["text_sent"] + details["text_failed"] > 0
    ):
        photo = "未生成可发送截图"
    cache_read = {
        "hit": "已恢复本账号记录",
        "miss": "未命中，改为检查网站",
        "invalid": "记录无效，改为检查网站",
        "restore_failed": "恢复失败，改为检查网站",
    }.get(environ.get("CACHE_STATUS", ""), "未完成")
    cache_save = _cache_save(environ, skipped)
    title = f"{'Hax' if platform == 'hax' else 'Woiden'} · {'账号 #' + str(index) if index else '账号配置'}"
    lines = [
        f"### {title}",
        "",
        f"**{headline}**",
        "",
        "| 检查项 | 结果 |",
        "| --- | --- |",
        f"| 续期 | {business} |",
        f"| Telegram 文字 | {text} |",
        f"| Telegram 截图 | {photo} |",
        f"| 缓存读取 | {cache_read} |",
        f"| 到期日保存 | {cache_save} |",
        "",
    ]
    if reason != "none":
        lines += [f"> {REASON_LABELS[reason]}", ""]
    failure_stage = {
        "login_failed": "登录网站",
        "vps_read_failed": "读取 VPS 信息",
        "renewal_failed": "执行续期",
        "renewal_unverified": "核对到期日",
        "captcha_failed": "验证码处理",
        "renewal_code_unavailable": "等待续期验证码",
        "warp_failed": "切换 WARP",
        "warp_registration_failed": "注册 WARP",
        "warp_connection_failed": "核实 WARP 连接",
        "warp_ip_query_failed": "查询 WARP 出口",
        "warp_new_exit_unavailable": "更换 WARP 出口",
    }.get(reason)
    if (
        details["text_failed"]
        or details["photo_failed"]
        or (skipped and environ.get("NOTIFY_STATUS") == "failed")
        or (skipped and environ.get("NOTIFY_OUTCOME") == "failure")
    ):
        lines += ["> 通知未全部发送成功，请检查 Telegram 配置和服务可用性。", ""]
    lines += [
        "<details>",
        "<summary>查看运行详情</summary>",
        "",
        "| 运行项 | 记录 |",
        "| --- | --- |",
        f"| 最后阶段 | {stage} |",
    ]
    if failure_stage:
        lines.append(f"| 失败或待核实位置 | {failure_stage} |")
    if runtime:
        seconds = runtime["elapsed_seconds"]
        duration = (
            f"{seconds // 60} 分 {seconds % 60} 秒"
            if seconds >= 60
            else f"{seconds} 秒"
        )
        lines.append(f"| 续期脚本耗时 | {duration} |")
    if summary:
        lines += [
            f"| 续期重试 | {summary['retries']} 次 |",
            f"| WARP 更换 IP | {summary['warp_swaps']} 次 |",
            f"| WARP 刷新 | {summary['warp_refresh']} 次 |",
        ]
    lines += [
        "",
        "通知按发送接口返回统计；缓存保存仅在确认本轮记录存在后显示成功。",
        "",
        "</details>",
        "",
    ]
    return "\n".join(lines)


def write_summary(content: str) -> None:
    """只在当前步骤写入摘要，不回写之前步骤的摘要文件。"""
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with Path(target).open("a", encoding="utf-8") as output:
            output.write(content)


def _number(value: str, limit: int) -> int | None:
    if value.isascii() and value.isdigit() and len(value) <= 8:
        number = int(value)
        if number <= limit:
            return number
    return None


def _notification(details: Mapping[str, Any], kind: str, unit: str) -> str:
    sent, failed = details[f"{kind}_sent"], details[f"{kind}_failed"]
    parts = []
    if sent:
        parts.append(f"已发送 {sent} {unit}")
    if failed:
        parts.append(f"失败 {failed} {unit}")
    return "，".join(parts) or "未执行"


def _cache_save(environ: Mapping[str, str], skipped: bool) -> str:
    if skipped or environ.get("STATE_STATUS") == "unchanged":
        return "无需更新"
    if environ.get("STATE_STATUS") == "no_observed_state":
        return (
            "无可靠新值，保留旧记录"
            if environ.get("CACHE_STATUS") == "hit"
            else "无可靠新值，未保存"
        )
    if environ.get("STATE_OUTCOME") == "failure":
        return "状态准备失败"
    if environ.get("STATE_STATUS") == "changed":
        if (
            environ.get("VERIFY_OUTCOME") == "success"
            and environ.get("VERIFY_HIT") == "true"
        ):
            return "已保存（已确认本轮缓存存在）"
        if environ.get("SAVE_OUTCOME") == "failure":
            return "保存失败"
        if environ.get("SAVE_OUTCOME") == "success":
            return "待确认（尚未确认本轮缓存存在）"
        return "未完成保存"
    return "未执行"

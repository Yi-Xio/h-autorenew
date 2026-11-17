"""仅传递固定阶段和数值结果，页面及异常原文不进入公开报告。"""

import json
import os
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any, Dict, Protocol
from urllib.parse import urlsplit

from scripts.accounts import state_key

STAGES = frozenset(
    {"preparing", "login", "reading_vps", "renewing", "notifying", "complete"}
)
COUNTERS = (
    "renewed",
    "skipped",
    "failed",
    "unverified",
    "no_vps",
    "retries",
    "warp_swaps",
    "warp_refresh",
)
MAX_COUNTER = 1_000_000
DETAIL_COUNTERS = (
    "account_failures",
    "text_sent",
    "text_failed",
    "photo_sent",
    "photo_failed",
)
WARP_FAILURE_REASONS = frozenset(
    {
        "warp_failed",
        "warp_registration_failed",
        "warp_connection_failed",
        "warp_ip_query_failed",
        "warp_new_exit_unavailable",
    }
)
REASONS = WARP_FAILURE_REASONS | frozenset(
    {
        "none",
        "login_failed",
        "vps_read_failed",
        "renewal_failed",
        "renewal_unverified",
        "captcha_failed",
        "renewal_code_unavailable",
    }
)
_details: Dict[str, Any] = {**dict.fromkeys(DETAIL_COUNTERS, 0), "reason": "none"}
_stage = "preparing"


class ScreenshotBrowser(Protocol):
    """截图策略需要的浏览器接口。"""

    def get_current_url(self) -> str: ...

    def get_page_source(self) -> str: ...


def screenshot_allowed(browser: ScreenshotBrowser, path: str, base_url: str) -> bool:
    """只允许已确认的 VPS 信息页截图，默认拒绝其他页面。"""
    if not Path(path).name.endswith("-vps_info.png"):
        return False
    try:
        current = urlsplit(browser.get_current_url())
        expected = urlsplit(base_url)
        source = browser.get_page_source().lower()
        return (
            current.scheme == "https"
            and current.netloc == expected.netloc
            and current.path.rstrip("/") == "/vps-info"
            and not current.query
            and not current.fragment
            and "valid until" in source
            and "hostname" in source
            and "status" in source
        )
    except Exception:
        return False


def iso_date(value: object) -> date | None:
    """只接受完整 ISO 日期。"""
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        parsed = date.fromisoformat(value)
        return parsed if parsed.isoformat() == value else None
    except ValueError:
        return None


def publish_progress(stage: str) -> None:
    """更新私有进度快照，不打印页面或异常。"""
    if stage not in STAGES or stage == "complete":
        raise ValueError("invalid_stage")
    if stage == "preparing":
        _details.update({**dict.fromkeys(DETAIL_COUNTERS, 0), "reason": "none"})
    _write_snapshot(stage, None)


def validate_details(value: object) -> Dict[str, Any]:
    """明细只有固定原因和有界计数，不接收页面或异常原文。"""
    if not isinstance(value, dict) or set(value) != {*DETAIL_COUNTERS, "reason"}:
        raise ValueError("invalid_details")
    if not isinstance(value["reason"], str) or value["reason"] not in REASONS:
        raise ValueError("invalid_details")
    for key in DETAIL_COUNTERS:
        if type(value[key]) is not int or not 0 <= value[key] <= MAX_COUNTER:
            raise ValueError("invalid_details")
    return dict(value)


def record_notification(kind: str, sent: bool) -> None:
    """每次完整发送操作记录一次结果，内部重试不重复计数。"""
    if kind not in {"text", "photo"} or type(sent) is not bool:
        raise ValueError("invalid_notification")
    key = f"{kind}_{'sent' if sent else 'failed'}"
    _details[key] += 1
    _write_snapshot(_stage, None)


def validate_snapshot(value: object) -> tuple[str, Dict[str, int] | None]:
    """校验 worker 报告，拒绝额外字段、自由文本和无效计数。"""
    if not isinstance(value, dict) or set(value) != {
        "version",
        "stage",
        "summary",
        "details",
    }:
        raise ValueError("invalid_report")
    if type(value["version"]) is not int or value["version"] != 2:
        raise ValueError("invalid_report")
    details = validate_details(value["details"])
    stage = value["stage"]
    if not isinstance(stage, str) or stage not in STAGES:
        raise ValueError("invalid_report")
    summary = value["summary"]
    if stage != "complete":
        if summary is not None:
            raise ValueError("invalid_report")
        return stage, None
    if not isinstance(summary, dict) or set(summary) != set(COUNTERS):
        raise ValueError("invalid_report")
    clean: Dict[str, int] = {}
    for key in COUNTERS:
        count = summary[key]
        if type(count) is not int or not 0 <= count <= MAX_COUNTER:
            raise ValueError("invalid_report")
        clean[key] = count
    if not 1 <= sum(clean[key] for key in COUNTERS[:5]) <= MAX_COUNTER:
        raise ValueError("invalid_report")
    if details["account_failures"] > clean["failed"]:
        raise ValueError("invalid_report")
    return stage, clean


# 上游结果字典包含页面信息等不同类型，只在此处提取允许公开的计数。
def renewal_verified(server: Mapping[str, Any]) -> bool:
    """只以信息页中实际向后延长的日期确认续期。"""
    old = iso_date(server.get("old_valid_until"))
    observed = iso_date(server.get("info_valid_until"))
    return bool(old and observed and observed > old)


def publish_results(results: Sequence[Mapping[str, Any]], platform: str) -> int:
    """生成保守的公开结果和私有状态值，返回业务退出码。"""
    if platform not in {"hax", "woiden"} or not results:
        raise ValueError("invalid_results")
    summary: Dict[str, int] = dict.fromkeys(COUNTERS, 0)
    _details.update(account_failures=0, reason="none")
    state_lines = []
    for account in results:
        servers = account.get("servers", [])
        if not servers:
            if account.get("public_state") == "no_vps":
                summary["no_vps"] += 1
                state_lines.append(f"{state_key(account['username'])},NOVPS")
            else:
                summary["failed"] += 1
                _details["account_failures"] += 1
                reason = account.get("public_reason", "vps_read_failed")
                if reason not in REASONS or reason == "none":
                    raise ValueError("invalid_reason")
                _details["reason"] = reason
            continue
        dates = []
        for server in servers:
            old = iso_date(server.get("old_valid_until"))
            observed = iso_date(server.get("info_valid_until"))
            if server.get("skipped") is True:
                summary["skipped"] += 1
            elif renewal_verified(server):
                summary["renewed"] += 1
            elif server.get("success") is True:
                summary["unverified"] += 1
                _details["reason"] = "renewal_unverified"
            else:
                summary["failed"] += 1
                reason = server.get("public_reason", "renewal_failed")
                if reason not in REASONS or reason == "none":
                    raise ValueError("invalid_reason")
                _details["reason"] = reason
            for source, target in (
                ("retry_count", "retries"),
                ("warp_swaps", "warp_swaps"),
                ("warp_refresh", "warp_refresh"),
            ):
                count = server.get(source, 0)
                if type(count) is not int or not 0 <= count <= MAX_COUNTER:
                    raise ValueError("invalid_counter")
                summary[target] += count
            # r["servers"] 是 renew 返回值；未能读取最终信息时只保留已知的原日期。
            dates.append(observed or old)
        if all(dates):
            earliest = min(item for item in dates if item is not None)
            state_lines.append(
                f"{state_key(account['username'])},{earliest.isoformat()}"
            )
    validate_snapshot(
        {"version": 2, "stage": "complete", "summary": summary, "details": _details}
    )
    if state_lines:
        Path(f"{platform}-time-value.txt").write_text(
            "\n".join(state_lines) + "\n", encoding="utf-8"
        )
    _write_snapshot("complete", summary)
    return 1 if summary["failed"] or summary["unverified"] else 0


def _write_snapshot(stage: str, summary: Dict[str, int] | None) -> None:
    global _stage
    _stage = stage
    target = os.environ.get("PUBLIC_REPORT_FILE")
    if not target:
        return
    path = Path(target)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {
                "version": 2,
                "stage": stage,
                "summary": summary,
                "details": validate_details(_details),
            }
        ),
        encoding="utf-8",
    )
    temporary.replace(path)

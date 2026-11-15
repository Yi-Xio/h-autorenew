"""账号矩阵、缓存预筛和跳过通知，不公开凭据或请求原文。"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from scripts.accounts import (
    parse_batch,
    public_account_id,
    register_masks,
    selected_account,
)
from scripts.public_report import iso_date
from scripts.state import CACHE_FILE, prepare_cached_state, read_cached_state


def build_matrix() -> None:
    """矩阵只传数字序号，完整凭据不进入任务名或步骤输出。"""
    accounts = parse_batch(os.environ.get("BATCH", ""))
    register_masks([field for account in accounts for field in account])
    _outputs({"matrix": json.dumps({"index": list(range(1, len(accounts) + 1))})})
    print(f"stage=matrix accounts={len(accounts)}")


def setup_cache(platform: str) -> None:
    """清理本地旧快照，输出本账号专用的恢复前缀和新快照 key。"""
    index, fields = selected_account(dict(os.environ))
    register_masks(fields)
    run = os.environ.get("GITHUB_RUN_ID", "1")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    if any(not re.fullmatch(r"[1-9][0-9]{0,24}", value) for value in (run, attempt)):
        raise ValueError("invalid_run")
    prefix = f"renewal-state-v1-{platform}-{public_account_id(platform, fields)}-"
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.unlink(missing_ok=True)
    CACHE_FILE.with_suffix(".tmp").unlink(missing_ok=True)
    _outputs({"cache_prefix": prefix, "cache_key": f"{prefix}{run}-{attempt}"})
    print(f"account={index} stage=cache result=prepared")


def check_account(platform: str) -> None:
    """按恢复的本账号日期预筛；缓存不可用时执行网站检查。"""
    index, fields = selected_account(dict(os.environ))
    register_masks(fields)
    skip_days = int(os.environ.get("SKIP_DAYS", "5" if platform == "hax" else "2"))
    if not 1 <= skip_days <= 365:
        raise ValueError("invalid_threshold")
    state = None
    if os.environ.get("CACHE_RESTORE_OUTCOME", "success") != "success":
        status = "restore_failed"
    else:
        try:
            state = read_cached_state(
                CACHE_FILE, platform, public_account_id(platform, fields)
            )
            status = "hit" if state is not None else "miss"
        except (ValueError, OSError, UnicodeError):
            status = "invalid"
    today = datetime.now(timezone(timedelta(hours=8))).date()
    valid = iso_date(state)
    days = (valid - today).days if valid else None
    needed = days is None or days < skip_days
    _outputs(
        {
            "should_run": "true" if needed else "false",
            "days_left": str(days) if days is not None else "",
            "cache_status": status,
        }
    )
    print(
        f"account={index} stage=check cache={status} result={'needed' if needed else 'skipped'}"
    )


def notify_skipped(platform: str) -> None:
    """只通知本任务跳过的账号，公开端不打印请求错误。"""
    index, fields = selected_account(dict(os.environ))
    register_masks(fields)
    days = int(os.environ["DAYS_LEFT"])
    if not 0 <= days <= 4_000_000:
        raise ValueError("invalid_days")
    phone, token, chat, *_ = fields
    text = f"{platform} 续期报告\n账号: ***{phone[-3:]}\n剩余 {days} 天，无需续期"
    try:
        request = Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=urlencode({"chat_id": chat, "text": text}).encode(),
        )
        with urlopen(request, timeout=15) as response:
            result = json.loads(response.read(65536))
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise ValueError("notification_failed")
        print(f"account={index} stage=notification result=sent")
    except Exception:
        # 通知失败不改变无需续期的业务结果。
        print(
            f"account={index} stage=notification result=failed category=notification_failed"
        )


def prepare_state(platform: str) -> None:
    """为官方保存 Action 准备快照；缺少可靠结果时不保存。"""
    index, fields = selected_account(dict(os.environ))
    register_masks(fields)
    status = prepare_cached_state(
        CACHE_FILE,
        Path(f"{platform}-time-value.txt"),
        platform,
        fields,
        restored=os.environ.get("CACHE_RESTORE_OUTCOME", "success") == "success",
    )
    _outputs(
        {"changed": "true" if status == "changed" else "false", "state_status": status}
    )
    print(f"account={index} stage=cache result={status}")


def _outputs(values: dict[str, str]) -> None:
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        for name, value in values.items():
            output.write(f"{name}={value}\n")


def main() -> int:
    """只允许固定命令和平台，失败不输出原始输入。"""
    try:
        if len(sys.argv) != 3 or sys.argv[2] not in {"hax", "woiden"}:
            raise ValueError("invalid_arguments")
        if (
            os.environ.get("GITHUB_ACTIONS") == "true"
            and os.environ.get("GITHUB_REF") != "refs/heads/master"
        ):
            raise ValueError("invalid_branch")
        match sys.argv[1]:
            case "matrix":
                build_matrix()
            case "setup-cache":
                setup_cache(sys.argv[2])
            case "check":
                check_account(sys.argv[2])
            case "notify-skipped":
                notify_skipped(sys.argv[2])
            case "prepare-state":
                prepare_state(sys.argv[2])
            case _:
                raise ValueError("invalid_arguments")
        return 0
    except Exception:
        print("result=failed category=workflow_step_failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())

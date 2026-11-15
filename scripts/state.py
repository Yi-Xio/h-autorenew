"""校验每账号缓存，只从本轮可靠结果生成下一份快照。"""

import json
import re
from collections.abc import Sequence
from pathlib import Path

from scripts.accounts import public_account_id, state_key
from scripts.public_report import iso_date

CACHE_FILE = Path("output/renewal-state.json")


def read_observed_state(path: Path, fields: Sequence[str]) -> str | None:
    """只接受受控入口导出的本账号可靠日期或无 VPS 状态。"""
    try:
        if path.stat().st_size > 256:
            raise ValueError("invalid_state")
        key, state = path.read_text(encoding="utf-8").strip().split(",")
    except FileNotFoundError:
        return None
    if key != state_key(fields[0]) or (state != "NOVPS" and iso_date(state) is None):
        raise ValueError("invalid_state")
    return state


def read_cached_state(path: Path, platform: str, account: str) -> str | None:
    """只接受本平台、本账号的固定结构；缓存缺失时返回 None。"""
    if platform not in {"hax", "woiden"} or not re.fullmatch(r"[a-f0-9]{64}", account):
        raise ValueError("invalid_state")
    try:
        if path.stat().st_size > 4096:
            raise ValueError("invalid_cache")
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "platform", "account", "state"}
        or type(value["version"]) is not int
        or value["version"] != 1
        or value["platform"] != platform
        or value["account"] != account
        or not isinstance(value["state"], str)
        or (value["state"] != "NOVPS" and iso_date(value["state"]) is None)
    ):
        raise ValueError("invalid_cache")
    return value["state"]


def prepare_cached_state(
    path: Path,
    observed: Path,
    platform: str,
    fields: Sequence[str],
    restored: bool = True,
) -> str:
    """校验本轮导出后按需更新本地快照，远端传输交给官方 Cache Action。"""
    account = public_account_id(platform, fields)
    state = read_observed_state(observed, fields)
    if state is None:
        return "no_observed_state"
    try:
        previous = read_cached_state(path, platform, account) if restored else None
    except (ValueError, OSError, UnicodeError):
        previous = None
    if previous == state:
        return "unchanged"
    snapshot = dict(version=1, platform=platform, account=account, state=state)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(snapshot, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    temporary.replace(path)
    return "changed"

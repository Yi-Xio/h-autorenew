"""仅以不透明账号标识公开到期日，Git 操作不转发原始输出。"""

import base64
import os
import re
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path

from scripts.accounts import public_account_id, state_key
from scripts.public_report import iso_date


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


def read_repository_state(path: Path) -> dict[str, str]:
    """读取公开行格式，旧手机号短索引不作为当前账号的记录。"""
    try:
        if path.stat().st_size > 65536:
            raise ValueError("invalid_state")
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}
    records = {}
    for line in lines:
        if not line.strip():
            continue
        key, state = line.strip().split(",")
        if state != "NOVPS" and iso_date(state) is None:
            raise ValueError("invalid_state")
        if re.fullmatch(r"[a-f0-9]{8}", key):
            continue
        if not re.fullmatch(r"[a-f0-9]{64}", key) or key in records:
            raise ValueError("invalid_state")
        records[key] = state
    return records


def save_repository_state(root: Path, platform: str, fields: Sequence[str]) -> str:
    """只更新 master 中本账号的公开行，并发冲突时重新读取后重试。"""
    account = public_account_id(platform, fields)
    filename = f"{platform}-time.txt"
    state = read_observed_state(root / f"{platform}-time-value.txt", fields)
    if state is None:
        return "no_observed_state"
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("GITHUB_", "ACTIONS_", "GIT_CONFIG_"))
        and key
        not in {
            "BATCH",
            "PROXY_CONTENT",
            "TWOCAPTCHA_API_KEY",
            "GH_TOKEN",
            "STATE_WRITE_TOKEN",
        }
    }
    environment["GIT_TERMINAL_PROMPT"] = "0"
    token = os.environ.get("STATE_WRITE_TOKEN", "")
    if token:
        auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        environment.update(
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
            GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {auth}",
        )
    remote = _git(root, ["remote", "get-url", "origin"], environment).strip()
    with tempfile.TemporaryDirectory(prefix="renewal-state-") as temporary:
        checkout = Path(temporary)
        _git(checkout, ["init", "--quiet"], environment)
        for _attempt in range(3):
            _git(
                checkout, ["fetch", "--quiet", remote, "refs/heads/master"], environment
            )
            _git(
                checkout,
                ["checkout", "--quiet", "--detach", "--force", "FETCH_HEAD"],
                environment,
            )
            path = checkout / filename
            records = read_repository_state(path)
            if records.get(account) == state:
                return "unchanged"
            records[account] = state
            path.write_text(
                "".join(f"{key},{records[key]}\n" for key in sorted(records)),
                encoding="utf-8",
            )
            _git(checkout, ["add", "--", filename], environment)
            _git(
                checkout,
                [
                    "-c",
                    "user.name=github-actions[bot]",
                    "-c",
                    "user.email=github-actions[bot]@users.noreply.github.com",
                    "commit",
                    "--quiet",
                    "-m",
                    f"chore: 更新 {platform} 到期日记录",
                ],
                environment,
            )
            try:
                _git(
                    checkout,
                    ["push", "--quiet", remote, "HEAD:refs/heads/master"],
                    environment,
                )
                return "saved"
            except subprocess.CalledProcessError:
                # 下次重新获取 master，仅修改自己的行，不覆盖其他账号的记录。
                continue
    raise ValueError("state_save_failed")


def _git(root: Path, arguments: list[str], environment: dict[str, str]) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=root,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
        timeout=60,
    ).stdout

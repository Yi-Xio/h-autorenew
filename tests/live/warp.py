"""检查真实 WARP 连接或严格换出口，不加载账号、浏览器或 Telegram。"""

import argparse
import ast
import contextlib
import json
import os
import subprocess
import sys
import time
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any

from scripts.public_report import WARP_FAILURE_REASONS


def main(arguments: list[str] | None = None) -> int:
    """输出固定结果和计数；网络操作未达成时返回非零。"""
    parser = argparse.ArgumentParser(description="仅验证 WARP，不执行续期。")
    parser.add_argument("--mode", choices=("ready", "rotate"), default="ready")
    parser.add_argument("--attempts", type=int, choices=range(1, 21), default=3)
    args = parser.parse_args(arguments)
    try:
        root = Path(__file__).resolve().parents[2]
        tree = ast.parse((root / "hax-renew.py").read_text())
        nodes: list[ast.stmt] = [
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "WarpManager"
        ]
        if len(nodes) != 1:
            raise ValueError("missing_warp_manager")
        # 从本候选加载实际类，避免导入旧认证、浏览器及可选 OCR 依赖。
        namespace: dict[str, Any] = dict(
            subprocess=subprocess,
            time=time,
            IPv4Address=IPv4Address,
            WARP_CMD_TIMEOUT=10,
        )
        exec(
            compile(ast.Module(body=nodes, type_ignores=[]), "hax-renew.py", "exec"),
            namespace,
        )
        with (
            open(os.devnull, "w") as quiet,
            contextlib.redirect_stdout(quiet),
            contextlib.redirect_stderr(quiet),
        ):
            manager = namespace["WarpManager"]()
            manager.record_initial_ip()
            completed = (
                manager.ensure_ready(max_attempts=args.attempts)
                if args.mode == "ready"
                else manager.rotate_ip(max_attempts=args.attempts)
            )
        reason = "none" if completed else manager.failure_reason
        if reason not in WARP_FAILURE_REASONS | {"none"}:
            raise ValueError("invalid_warp_reason")
        print(
            json.dumps(
                dict(
                    mode=args.mode,
                    result=("ready" if args.mode == "ready" else "changed")
                    if completed
                    else "failed",
                    reason=reason,
                    warp_swaps=manager.rotation_count,
                    warp_refresh=manager.warp_refresh_count,
                )
            )
        )
        return 0 if completed else 1
    except Exception:
        print(json.dumps(dict(mode=args.mode, result="failed", reason="warp_failed")))
        return 1


if __name__ == "__main__":
    sys.exit(main())

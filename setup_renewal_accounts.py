"""将已导出的私有会话打包成账号数组，或离线检查账号配置。"""

import sys
from pathlib import Path

from scripts.renewal_config import pack_accounts, validate_accounts
from scripts.web_sessions import LOCAL_ROOT, load_private_json, save_private_json
from setup_web_sessions import SessionParser


def main(arguments: list[str] | None = None) -> int:
    """只输出固定结果，不回显账号、会话、路径或解析错误。"""
    try:
        parser = SessionParser(
            description="离线打包或校验 Hax 账号数组，不连接 Telegram。"
        )
        parser.add_argument("operation", choices=("pack", "check"))
        parser.add_argument("--input", type=Path)
        parser.add_argument("--output", type=Path)
        args = parser.parse_args(arguments)
        args.input = args.input or LOCAL_ROOT / "secrets" / (
            "manifest.json" if args.operation == "pack" else "accounts.json"
        )
        args.output = args.output or LOCAL_ROOT / "secrets/accounts.json"
        if args.operation == "check":
            validate_accounts(load_private_json(args.input))
            print("result=valid kind=accounts online=not_checked")
        else:
            if args.output is None:
                raise ValueError("missing_output")
            save_private_json(args.output, pack_accounts(args.input))
            print("result=packed kind=accounts online=not_checked")
        return 0
    except Exception:
        print("result=failed category=invalid_private_configuration")
        return 1


if __name__ == "__main__":
    sys.exit(main())

"""从专用浏览器导出手动登录态，或离线检查已经保存的会话。"""

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Never
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from scripts.hax_login import read_existing_authorization
from scripts.renewal_config import identifier_directory

try:
    from DrissionPage import Chromium
except ImportError:
    Chromium = None

from scripts.web_sessions import (
    ERROR_MESSAGES,
    HAX_CLIENT,
    HAX_ORIGIN,
    HAX_LOGIN_ORIGIN,
    TELEGRAM_CLIENT,
    TELEGRAM_ORIGIN,
    LOCAL_ROOT,
    Session,
    SessionError,
    AuthenticationError,
    hax_snapshot,
    load_snapshot,
    phone_digits,
    save_snapshot,
    telegram_snapshot,
    validate_snapshot,
)


class SessionParser(argparse.ArgumentParser):
    """错误参数不原样回显，避免误填凭据时再次输出。"""

    def error(self, message: str) -> Never:
        raise SessionError("invalid_arguments")


def profile_address(profile: Path) -> str:
    """核对资料目录中的浏览器标识，避免旧端口指向另一实例。"""
    try:
        marker = profile / "DevToolsActivePort"
        if marker.stat().st_size > 1024:
            raise SessionError("browser_unavailable")
        lines = marker.read_text().splitlines()
        if len(lines) != 2 or not lines[1].startswith("/devtools/browser/"):
            raise SessionError("browser_unavailable")
        port = int(lines[0])
        if not 1 <= port <= 65535:
            raise SessionError("browser_unavailable")
        opener = build_opener(ProxyHandler({}))
        with opener.open(
            f"http://127.0.0.1:{port}/json/version", timeout=5
        ) as response:
            raw = response.read(65537)
        if len(raw) > 65536:
            raise SessionError("browser_unavailable")
        data = json.loads(raw)
        target = urlsplit(data.get("webSocketDebuggerUrl", ""))
        if (
            target.scheme != "ws"
            or target.hostname != "127.0.0.1"
            or target.port != port
            or target.path != lines[1]
            or target.username
            or target.password
            or target.query
            or target.fragment
        ):
            raise SessionError("browser_unavailable")
        return f"127.0.0.1:{port}"
    except SessionError:
        raise
    except Exception:
        raise SessionError("browser_unavailable") from None


def export_from_page(
    page: Any,
    kind: str,
    phone: str,
    telegram: object = None,
    confirmed_hax_account: bool = False,
) -> Session:
    """读取已打开的目标页面；浏览器动态返回在会话模块再次校验。"""
    # DrissionPage 的动态页面对象没有覆盖此处返回值的静态类型。
    try:
        address = page.url
        if not isinstance(address, str):
            raise SessionError("page_unverified")
        parsed = urlsplit(address)
        invalid = parsed.username or parsed.password or parsed.port not in {None, 443}
    except (TypeError, ValueError):
        raise SessionError("page_unverified") from None
    if invalid:
        raise SessionError("page_unverified")
    origin = f"{parsed.scheme}://{parsed.hostname}"
    if kind == "telegram":
        if origin != TELEGRAM_ORIGIN or parsed.path != "/k/":
            raise SessionError("page_unverified")
        state = page.run_js("""
            const ready = document.querySelector('#page-chats');
            const auth = document.querySelector('#auth-pages');
            if (!ready || !ready.getClientRects().length ||
                (auth && auth.getClientRects().length)) return null;
            return Object.fromEntries([1, 2, 3, 4]
                .map(index => {
                    const key = `account${index}`;
                    return [key, localStorage.getItem(key)];
                }).filter(([, value]) => value !== null));
        """)
        if state is None:
            raise SessionError("not_logged_in")
        return telegram_snapshot(state, phone)
    if (
        kind != "hax"
        or origin != HAX_ORIGIN
        or parsed.path.rstrip("/") not in {"/login", "/vps-info"}
    ):
        raise SessionError("page_unverified")
    telegram = validate_snapshot(
        telegram, expected_client=TELEGRAM_CLIENT, expected_phone=phone_digits(phone)
    )
    if not confirmed_hax_account:
        raise SessionError("confirmation_required")
    try:
        read_existing_authorization(page, telegram["account"]["userId"])
        cookies = page.run_cdp("Network.getCookies", urls=[HAX_LOGIN_ORIGIN + "/"])[
            "cookies"
        ]
    except AuthenticationError as error:
        raise SessionError(
            error.reason if error.reason in ERROR_MESSAGES else "page_unverified"
        ) from None
    return hax_snapshot(
        cookies,
        telegram,
        confirmed_account=confirmed_hax_account,
    )


def main(arguments: list[str] | None = None) -> int:
    """只输出固定结果和原因，不打印会话内容或浏览器诊断。"""
    try:
        parser = SessionParser(
            description="导出专用浏览器中的手动登录态，不执行登录或续期。"
        )
        parser.add_argument("operation", choices=("export", "check"))
        parser.add_argument("--kind", choices=("telegram", "hax"), required=True)
        parser.add_argument("--phone", required=True, help="+国家码-号码")
        parser.add_argument(
            "--account-identifier", default="A001", help="账号标识符，例如 hax.co.id.us"
        )
        parser.add_argument(
            "--profile", type=Path, help="专用 Chrome 资料目录，包含 DevToolsActivePort"
        )
        parser.add_argument(
            "--output",
            type=Path,
            help="默认 .local/secrets/<派生目录名>/<类型>.json，不覆盖已有文件",
        )
        parser.add_argument("--input", type=Path, help="需要离线检查的私有会话文件")
        parser.add_argument(
            "--telegram", type=Path, help="Hax 配对使用的 Telegram 会话文件"
        )
        parser.add_argument(
            "--confirm-hax-account",
            action="store_true",
            help="确认 Hax 属于 --phone 对应账号",
        )
        args = parser.parse_args(arguments)
        directory = identifier_directory(args.account_identifier)
        account_directory = LOCAL_ROOT / "secrets" / directory
        args.output = args.output or account_directory / (args.kind + ".json")
        args.profile = args.profile or LOCAL_ROOT / "browser" / directory
        if args.kind == "hax":
            args.telegram = args.telegram or account_directory / "telegram.json"
        phone = phone_digits(args.phone)
        client = TELEGRAM_CLIENT if args.kind == "telegram" else HAX_CLIENT
        telegram = None
        if args.kind == "hax":
            if args.telegram is None:
                raise SessionError("invalid_arguments")
            telegram = load_snapshot(
                args.telegram, expected_client=TELEGRAM_CLIENT, expected_phone=phone
            )
        if args.operation == "check":
            if args.input is None:
                raise SessionError("invalid_arguments")
            expected = dict(expected_client=client, expected_phone=phone)
            if telegram is not None:
                expected["expected_user_id"] = telegram["account"]["userId"]
            load_snapshot(args.input, **expected)
            print(f"result=valid kind={args.kind} online=not_checked")
            return 0
        if os.environ.get("GITHUB_ACTIONS") == "true":
            raise SessionError("local_only")
        if args.kind == "hax" and not args.confirm_hax_account:
            raise SessionError("confirmation_required")
        if Chromium is None:
            raise SessionError("dependency_missing")
        try:
            address = profile_address(args.profile)
            # 屏蔽连接库的 Python 标准输出和错误输出，命令行只报告固定结果。
            with (
                open(os.devnull, "w") as quiet,
                contextlib.redirect_stdout(quiet),
                contextlib.redirect_stderr(quiet),
            ):
                browser = Chromium(address)
                pages = []
                for page in browser.get_tabs():
                    address = page.url
                    if not isinstance(address, str):
                        continue
                    location = urlsplit(address)
                    if location.hostname == (
                        "web.telegram.org" if args.kind == "telegram" else "hax.co.id"
                    ) and location.path.rstrip("/") in (
                        {"/k"} if args.kind == "telegram" else {"/login", "/vps-info"}
                    ):
                        pages.append(page)
                if len(pages) != 1:
                    raise SessionError("page_unverified")
                snapshot = export_from_page(
                    pages[0], args.kind, args.phone, telegram, args.confirm_hax_account
                )
        except SessionError:
            raise
        except Exception:
            raise SessionError("browser_unavailable") from None
        save_snapshot(args.output, snapshot)
        online = "authorization_verified" if args.kind == "hax" else "not_checked"
        print(f"result=exported kind={args.kind} online={online}")
        return 0
    except SessionError as error:
        print(f"result=failed reason={error.code} {ERROR_MESSAGES[error.code]}")
        return 1
    except Exception:
        print("result=failed reason=file_error 会话处理失败，请检查本机配置。")
        return 1


if __name__ == "__main__":
    sys.exit(main())

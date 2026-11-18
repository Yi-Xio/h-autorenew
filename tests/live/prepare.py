"""在 Docker 桌面中手动准备各账号，复用正式导出和配置校验。"""

import contextlib
import os
import signal
import stat
import subprocess
import sys
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from DrissionPage import Chromium

from scripts.browser_setup import install_hax_rules
from scripts.renewal_config import (
    identifier_directory,
    notification_config,
    pack_accounts,
    validate_accounts,
    validate_identifier,
)
from scripts.web_sessions import (
    AuthenticationError,
    ERROR_MESSAGES,
    LOCAL_ROOT,
    SessionError,
    load_private_json,
    private_output_path,
    save_private_json,
)
from setup_web_sessions import SessionParser, export_from_page, profile_address

SECRETS = LOCAL_ROOT / "secrets"


def store_configuration(accounts: object, token: str, chat: str) -> None:
    """所有新文件校验通过后归档旧版本，再替换当前配置。"""
    checked = validate_accounts(accounts)
    notification_config(dict(TELEGRAM_BOT_TOKEN=token, TELEGRAM_CHAT_ID=chat))
    payloads = {
        "accounts.json": dict(version=1, accounts=checked),
        "notification-token.json": token,
        "notification-chat.json": chat,
        "manifest.json": dict(
            version=1,
            accounts=[
                dict(
                    id=item["id"],
                    phone=item["phone"],
                    telegram_web_file=f"{identifier_directory(item['id'])}/telegram.json",
                    hax_web_file=f"{identifier_directory(item['id'])}/hax.json",
                )
                for item in checked
            ],
        ),
    }
    for item in checked:
        directory = identifier_directory(item["id"])
        payloads[f"{directory}/telegram.json"] = item["telegram_web"]
        payloads[f"{directory}/hax.json"] = item["hax_web"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    staging = SECRETS / "pending" / stamp
    archive = SECRETS / "archive" / stamp
    replaced: list[tuple[Path, Path | None]] = []
    try:
        for name, value in payloads.items():
            save_private_json(staging / name, value)
            target = private_output_path(SECRETS / name)
            if target.exists():
                load_private_json(target)
        for name in payloads:
            target = SECRETS / name
            destination = None
            if target.exists():
                # 先创建受同一保护约束的归档路径，保留旧文件的原始字节。
                destination = archive / name
                save_private_json(destination, None)
                target.replace(destination)
            replaced.append((target, destination))
            if not target.parent.exists():
                target.parent.mkdir(mode=0o700)
            (staging / name).replace(target)
    except BaseException:
        for target, backup in reversed(replaced):
            target.unlink(missing_ok=True)
            if backup is not None:
                backup.replace(target)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        with contextlib.suppress(OSError):
            staging.parent.rmdir()


def check_configuration() -> int:
    """离线核对已保存的三项配置，不打开网页或发送通知。"""
    accounts = validate_accounts(load_private_json(SECRETS / "accounts.json"))
    if pack_accounts(SECRETS / "manifest.json") != dict(version=1, accounts=accounts):
        raise ValueError("manifest_mismatch")
    notification_config(
        {
            name + "_FILE": str(SECRETS / filename)
            for name, filename in (
                ("TELEGRAM_BOT_TOKEN", "notification-token.json"),
                ("TELEGRAM_CHAT_ID", "notification-chat.json"),
            )
        }
    )
    print(f"result=valid accounts={len(accounts)} online=not_checked")
    return 0


def prepare_account(identity: str, phone: str) -> dict:
    """用户手动完成登录后，只读取同账号的两份最小状态。"""
    profile = LOCAL_ROOT / "browser" / identifier_directory(identity)
    private_output_path(profile / "permission-check.json")
    profile.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = profile.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise ValueError("unsafe_browser_profile")
    with open(os.devnull, "w") as quiet:
        browser_process = subprocess.Popen(
            [
                "google-chrome",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--no-default-browser-check",
                "--password-store=basic",
                "--remote-debugging-address=127.0.0.1",
                "--remote-debugging-port=0",
                f"--user-data-dir={profile}",
                "about:blank",
            ],
            stdout=quiet,
            stderr=quiet,
        )
        browser = None
        try:
            print("正在启动浏览器并准备 Hax 页面规则。")
            deadline = time.monotonic() + 30
            with contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
                while True:
                    if browser_process.poll() is not None:
                        raise AuthenticationError("browser_unavailable")
                    try:
                        address = profile_address(profile)
                        break
                    except SessionError:
                        if time.monotonic() >= deadline:
                            raise AuthenticationError("browser_unavailable") from None
                        time.sleep(0.2)
                browser = Chromium(address)
                page = browser.new_tab("about:blank")
                install_hax_rules(page)
                page.get("https://hax.co.id/login")
                browser.new_tab("https://web.telegram.org/k/#@HaxTG_bot")
            print("请在容器桌面手动登录同一个 Telegram 和 Hax 账号。")
            input("确认账号无误，保留这两个页面，完成后按回车导出：")
            with contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
                telegram_pages, hax_pages = [], []
                for page in browser.get_tabs():
                    address = page.url
                    if not isinstance(address, str):
                        continue
                    if address.startswith("https://web.telegram.org/k/"):
                        telegram_pages.append(page)
                    if address.split("?")[0].rstrip("/") in {
                        "https://hax.co.id/login",
                        "https://hax.co.id/vps-info",
                    }:
                        hax_pages.append(page)
                if len(telegram_pages) != 1 or len(hax_pages) != 1:
                    raise ValueError("ambiguous_business_pages")
                telegram = export_from_page(telegram_pages[0], "telegram", phone)
                hax = export_from_page(hax_pages[0], "hax", phone, telegram, True)
            return dict(id=identity, phone=phone, telegram_web=telegram, hax_web=hax)
        finally:
            if browser is not None:
                with contextlib.suppress(Exception):
                    browser.quit(timeout=5, force=True)
            browser_process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                browser_process.wait(timeout=5)
            if browser_process.poll() is None:
                browser_process.kill()
                browser_process.wait()


def main(arguments: list[str] | None = None) -> int:
    """交互输入均可见；完成后只报告固定结果，不打印底层浏览器错误。"""

    def cancelled(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, cancelled)
    try:
        parser = SessionParser(
            description="Docker 内准备凭据，个人 Telegram 不执行脚本写入。"
        )
        parser.add_argument("--check", action="store_true")
        args = parser.parse_args(arguments)
        if args.check:
            return check_configuration()
        if not sys.stdin.isatty():
            raise ValueError("interactive_terminal_required")
        current = SECRETS / "accounts.json"
        accounts = (
            validate_accounts(load_private_json(current)) if current.exists() else []
        )
        while True:
            identity = input("本次准备的账号标识符，例如 hax.co.id.us：")
            try:
                validate_identifier(identity)
            except ValueError:
                print("账号标识符不能为空，且必须是有效文本，请重新输入。")
                continue
            if any(item["id"] == identity for item in accounts):
                if (
                    input("此账号标识符已存在，输入 UPDATE 确认更新并归档旧凭据：")
                    != "UPDATE"
                ):
                    raise ValueError("update_not_confirmed")
            phone = input("手机号，格式 +国家码-号码：")
            item = prepare_account(identity, phone)
            accounts = [old for old in accounts if old["id"] != identity] + [item]
            validate_accounts(dict(version=1, accounts=accounts))
            token_path = SECRETS / "notification-token.json"
            chat_path = SECRETS / "notification-chat.json"
            if (
                token_path.exists()
                and chat_path.exists()
                and input("保留原通知配置？[Y/n] ").lower() != "n"
            ):
                token = load_private_json(token_path)
                chat = load_private_json(chat_path)
            else:
                token = input("通知 Bot Token：")
                chat = input("通知 Chat ID：")
            if not isinstance(token, str) or not isinstance(chat, str):
                raise ValueError("invalid_notification")
            store_configuration(dict(version=1, accounts=accounts), token, chat)
            print("凭据已保存并校验，旧版本保留在 .local/secrets/archive/。")
            if input("继续准备另一个账号？[y/N] ").lower() != "y":
                return check_configuration()
    except (KeyboardInterrupt, EOFError):
        print("result=cancelled category=preparation_cancelled")
        return 130
    except AuthenticationError as error:
        message = ERROR_MESSAGES.get(error.reason, "浏览器准备失败，请检查容器和网络。")
        print(f"result=failed category={error.reason} {message}")
        return 1
    except Exception:
        print("result=failed category=preparation_failed，请检查配置、权限和业务页面。")
        return 1


if __name__ == "__main__":
    sys.exit(main())

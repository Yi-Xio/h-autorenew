"""用正式 Actions 入口实测，缓存传输仅在本地文件间模拟。"""

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from scripts.accounts import public_account_id, runtime_account
from scripts.job_summary import (
    AUTHENTICATION_FILE,
    validate_authentication_result,
    validate_runtime,
)
from scripts.renewal_config import (
    cache_identity,
    configured_accounts,
    identifier_directory,
    notification_config,
)
from scripts.state import CACHE_FILE, read_cached_state
from scripts.web_sessions import MAX_SESSION_BYTES, load_private_json, save_private_json
from setup_web_sessions import SessionParser

ROOT = Path(__file__).resolve().parents[2]
ACCOUNT_TIMEOUT_SECONDS = 1200
RUNTIME_TIMEOUT_SECONDS = ACCOUNT_TIMEOUT_SECONDS + 40


def import_configuration(source: Path, private: Path, owner: int) -> dict[str, str]:
    """校验只读挂载的所有者及权限，再生成当前容器用户的私有副本。"""
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(
            ("GITHUB_", "ACTIONS_", "RENEWAL_", "TELEGRAM_", "TG_", "PUBLIC_", "HAX_")
        )
        and name not in {"BATCH", "GH_TOKEN", "PROXY_CONTENT", "TWOCAPTCHA_API_KEY"}
    }
    for name, filename in (
        ("RENEWAL_ACCOUNTS", "accounts.json"),
        ("TELEGRAM_BOT_TOKEN", "notification-token.json"),
        ("TELEGRAM_CHAT_ID", "notification-chat.json"),
    ):
        original = source / filename
        info = source.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_mode & 0o077
            or info.st_uid != owner
        ):
            raise ValueError("unsafe_mount")
        descriptor = os.open(original, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != owner
                or info.st_mode & 0o077
                or info.st_size > MAX_SESSION_BYTES
            ):
                raise ValueError("unsafe_credentials")
            raw = stream.read(MAX_SESSION_BYTES + 1)
        target = private / filename
        with target.open("xb") as output:
            output.write(raw)
        target.chmod(0o600)
        value = load_private_json(target)
        if name == "RENEWAL_ACCOUNTS":
            environment[name] = json.dumps(value, separators=(",", ":"))
        elif isinstance(value, str):
            environment[name] = value
        else:
            raise ValueError("invalid_notification")
    configured_accounts(environment)
    notification_config(environment)
    return environment


def invoke(
    module: str, args: list[str], environment: dict[str, str], timeout: int = 60
) -> int:
    """启动正式命令，取消和超时均交给子进程清理后退出。"""
    with subprocess.Popen(
        [sys.executable, "-m", module, *args], env=environment, cwd=ROOT
    ) as child:
        try:
            return child.wait(timeout=timeout)
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


def workflow(operation: str, environment: dict[str, str]) -> tuple[int, dict[str, str]]:
    """读取正式 workflow 命令生成的固定步骤输出。"""
    output = Path(environment["GITHUB_OUTPUT"])
    output.unlink(missing_ok=True)
    code = invoke("scripts.workflow", [operation, "hax"], environment)
    values = (
        dict(line.split("=", 1) for line in output.read_text().splitlines())
        if output.exists()
        else {}
    )
    return code, values


def connect_warp() -> None:
    """使用镜像内的官方 client，核实实际流量经过 WARP。"""

    def command(args: list[str]) -> bool:
        return (
            subprocess.run(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
            ).returncode
            == 0
        )

    for _attempt in range(20):
        if command(["warp-cli", "--accept-tos", "status"]):
            break
        time.sleep(1)
    for args in (
        ["disconnect"],
        ["registration", "delete"],
        ["registration", "new"],
        ["mode", "warp+doh"],
        ["tunnel", "ip", "add-range", "::0/0"],
        ["connect"],
    ):
        succeeded = command(["warp-cli", "--accept-tos", *args])
        if not succeeded and args not in (["disconnect"], ["registration", "delete"]):
            raise ValueError("warp_setup_failed")
    for _attempt in range(20):
        result = subprocess.run(
            [
                "curl",
                "-4",
                "-fsS",
                "--max-time",
                "5",
                "https://www.cloudflare.com/cdn-cgi/trace",
            ],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        if result.returncode == 0 and {"warp=on", "warp=plus"}.intersection(
            result.stdout.splitlines()
        ):
            print("stage=warp result=connected", flush=True)
            return
        time.sleep(2)
    raise ValueError("warp_setup_failed")


def run_account(
    environment: dict[str, str],
    identity: str,
    index: int,
    mode: str,
    destination: Path,
    state_directory: Path,
    *,
    force: bool = False,
) -> int:
    """按所选命令执行一个账号，为本轮保存独立的回执。"""
    matches = [
        account
        for account in configured_accounts(environment)
        if account["id"] == identity
    ]
    if len(matches) != 1:
        raise ValueError("invalid_account_selection")
    env = dict(
        environment,
        RENEWAL_ACCOUNT_KEY=cache_identity(matches[0]),
        BATCH_ACCOUNT_INDEX=str(index),
        PUBLIC_SUMMARY_DEFERRED="1",
        PUBLIC_RUN_TIMEOUT_SECONDS=str(ACCOUNT_TIMEOUT_SECONDS),
        PUBLIC_HEARTBEAT_SECONDS="15",
        SKIP_DAYS="5",
        HAX_MIN_VALID_DAYS="5",
        MAX_WARP_ROTATE_ATTEMPTS="3",
        DEBUG_FLAG="0",
    )
    env.pop("RENEWAL_ACCOUNT_ID", None)
    env.pop("HAX_FORCE_RENEWAL", None)
    if force:
        env["HAX_FORCE_RENEWAL"] = "1"
    destination.mkdir(mode=0o700, parents=True)
    env.update(
        GITHUB_OUTPUT=str(destination / "step-output.txt"),
        GITHUB_STEP_SUMMARY=str(destination / "summary.md"),
    )
    receipt: dict[str, object] = dict(
        version=1,
        mode=mode,
        account_identifier=identity,
        input="actions",
        force=force,
        github_cache_uploaded=False,
        result="failed",
        validation="failed",
        reason="local_validation_failed",
    )
    try:
        if mode == "check-auth":
            # 与正式续期使用同一 WARP 准备步骤，避免直连通过被误当作正式环境可用。
            connect_warp()
            code = invoke(
                "scripts.actions_runtime", ["hax", "--check-auth"], env, RUNTIME_TIMEOUT_SECONDS
            )
            authentication = validate_authentication_result(
                json.loads((ROOT / AUTHENTICATION_FILE).read_text())
            )
            if authentication["status"] != "valid":
                code = code or 1
            receipt.update(
                result="authenticated" if code == 0 else "failed",
                validation="passed" if code == 0 else "failed",
                exit_code=code,
                reason=authentication["reason"]
                if authentication["reason"] != "none" or not code
                else "worker_error",
                checks=authentication["checks"],
            )
            return code
        code, _ = workflow("setup-cache", env)
        if code:
            raise ValueError("configuration_failed")
        fields = runtime_account("hax", env, with_notification=False)
        opaque = public_account_id("hax", fields)
        cached = state_directory / (opaque + ".json")
        if force:
            decision = {"should_run": "true", "cache_status": "forced"}
        else:
            if cached.exists():
                info = cached.lstat()
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("unsafe_cache")
                with cached.open("rb") as source:
                    raw = source.read(4097)
                # 损坏或过大的缓存交给正式预筛判定为 invalid，再回到网站检查。
                (ROOT / CACHE_FILE).write_bytes(raw)
            code, decision = workflow("check", env)
            if code:
                raise ValueError("cache_check_failed")
        should_run = decision.get("should_run") == "true"
        state = {}
        code = 0
        if should_run:
            connect_warp()
            code = invoke("scripts.actions_runtime", ["hax"], env, RUNTIME_TIMEOUT_SECONDS)
            receipt["renewal_exit_code"] = code
            state_code, state = workflow("prepare-state", env)
            if state_code:
                raise ValueError("state_preparation_failed")
        else:
            env["DAYS_LEFT"] = decision.get("days_left", "")
            code, notice = workflow("notify-skipped", env)
            env["NOTIFY_STATUS"] = notice.get("notification_status", "")
            if notice.get("notification_status") != "sent":
                code = 1
        summary_path = Path(env["GITHUB_STEP_SUMMARY"])
        summary_path.write_text(
            "> 本地真实验证；缓存保存仅在本地文件间模拟，GitHub 缓存服务未执行。\n\n"
        )
        if state.get("state_status") in {"changed", "unchanged"}:
            value = read_cached_state(ROOT / CACHE_FILE, "hax", opaque)
            if value is None:
                raise ValueError("state_missing")
            source = load_private_json(ROOT / CACHE_FILE)
            temporary = state_directory / (opaque + ".pending.json")
            save_private_json(temporary, source)
            temporary.replace(cached)
            receipt["local_cache_saved"] = True
        env.update(
            CHECKOUT_OUTCOME="success",
            CACHE_KEY_OUTCOME="success",
            CHECK_OUTCOME="success",
            SHOULD_RUN="true" if should_run else "false",
            CACHE_STATUS=decision.get("cache_status", ""),
            DAYS_LEFT=decision.get("days_left", ""),
            PYTHON_OUTCOME="success",
            DEPENDENCIES_OUTCOME="success",
            WARP_OUTCOME="success" if should_run else "skipped",
            PROXY_OUTCOME="success" if should_run else "skipped",
            RENEW_OUTCOME=("success" if code == 0 else "failure")
            if should_run
            else "skipped",
            STATE_OUTCOME="success" if state else "skipped",
            STATE_STATUS=state.get("state_status", ""),
            SAVE_OUTCOME="skipped",
            VERIFY_OUTCOME="skipped",
            VERIFY_HIT="",
        )
        summary_code, _values = workflow("summary", env)
        if summary_code:
            raise ValueError("summary_failed")
        runtime_path = ROOT / "output/renewal-result.json"
        if should_run:
            runtime = validate_runtime(json.loads(runtime_path.read_text()))
            counters, details = runtime["summary"], runtime["details"]
            receipt.update(
                counters=counters,
                notifications={
                    key: value
                    for key, value in details.items()
                    if key not in {"navigation", "checks"}
                },
            )
            if "navigation" in details:
                receipt["navigation"] = details["navigation"]
            if "checks" in details:
                receipt["checks"] = details["checks"]
            if details["text_failed"] or details["photo_failed"]:
                code = 1
            if force and (
                not counters
                or counters["renewed"] < 1
                or counters["failed"]
                or counters["unverified"]
                or details["text_sent"] < 1
                or details["photo_sent"] < 1
                or not receipt.get("local_cache_saved")
            ):
                code = 1
            business = "failed"
            if counters:
                for field in ("unverified", "failed", "renewed", "skipped", "no_vps"):
                    if counters[field]:
                        business = field
                        break
            receipt.update(
                result=business,
                validation="passed"
                if code == 0
                else "incomplete"
                if business in {"skipped", "no_vps"}
                else "failed",
                reason="notification_failed"
                if details["text_failed"] or details["photo_failed"]
                else runtime["reason"],
                exit_code=code,
            )
        else:
            receipt.update(
                result="skipped" if code == 0 else "failed",
                validation="passed" if code == 0 else "failed",
                reason="none" if code == 0 else "notification_failed",
                exit_code=code,
            )
        return code
    except (KeyboardInterrupt, subprocess.TimeoutExpired) as error:
        cancelled = isinstance(error, KeyboardInterrupt)
        receipt.update(
            result="cancelled" if cancelled else "failed",
            validation="cancelled" if cancelled else "failed",
            reason="local_validation_cancelled"
            if cancelled
            else "local_validation_timeout",
            exit_code=130 if cancelled else 1,
        )
        with (destination / "summary.md").open("a") as output:
            output.write(
                "\n\n**本地验证已取消，未完成本轮检查。**\n"
                if cancelled
                else "\n\n**本地验证已超时，未完成本轮检查。**\n"
            )
        raise
    finally:
        # 仅导出固定结构、计数和匿名 Summary，不导出页面、原始日志或聊天。
        save_private_json(destination / "receipt.json", receipt)
        (destination / "step-output.txt").unlink(missing_ok=True)
        shutil.rmtree(ROOT / "output", ignore_errors=True)
        (ROOT / "hax-time-value.txt").unlink(missing_ok=True)


def main(arguments: list[str] | None = None) -> int:
    """缺少凭据、运行失败及中断均返回非零，不伪装成完整实测通过。"""

    def cancelled(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    try:
        parser = SessionParser(description="在 Docker 中验证 Hax 续期和登录。", allow_abbrev=False)
        commands = parser.add_subparsers(dest="command")
        renew = commands.add_parser("renew", help="按生产策略运行一次续期任务", allow_abbrev=False)
        renew.add_argument("--force", action="store_true", help="跳过本地到期判断，实测续期")
        check_auth = commands.add_parser("check-auth", help="检查已保存的登录凭据", allow_abbrev=False)
        check_auth.set_defaults(force=False)
        for command in (renew, check_auth):
            command.add_argument(
                "--account-identifier",
                action="append",
                help="按原文选择账号标识符，可重复指定",
            )
        args = parser.parse_args(arguments)
        if args.command is None:
            parser.print_help()
            return 0
        signal.signal(signal.SIGTERM, cancelled)
        with tempfile.TemporaryDirectory(prefix="hax-live-") as temporary:
            env = import_configuration(
                Path("/mnt/hax-secrets"),
                Path(temporary),
                int(os.environ["HAX_DATA_UID"]),
            )
            accounts = configured_accounts(env)
            selected = args.account_identifier or [item["id"] for item in accounts]
            if len(set(selected)) != len(selected) or set(selected) - {
                item["id"] for item in accounts
            }:
                raise ValueError("invalid_account_selection")
            stamp = str(time.time_ns())
            exit_code = 0
            for index, account in enumerate(accounts, 1):
                if account["id"] in selected:
                    result = run_account(
                        env,
                        account["id"],
                        index,
                        args.command,
                        Path("/mnt/hax-results")
                        / stamp
                        / identifier_directory(account["id"]),
                        Path("/mnt/hax-state"),
                        force=args.force,
                    )
                    if result != 0:
                        exit_code = 1
            return exit_code
    except KeyboardInterrupt:
        print("result=cancelled category=local_validation_cancelled")
        return 130
    except Exception:
        print("result=failed category=local_validation_failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())

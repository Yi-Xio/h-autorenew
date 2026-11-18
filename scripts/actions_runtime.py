"""公开 Actions 的续期入口，原脚本输出只进入空设备。"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

from scripts.accounts import Account, register_masks, runtime_account, state_key
from scripts.web_sessions import save_private_json
from scripts.job_summary import (
    AUTHENTICATION_FILE,
    RUNTIME_FILE,
    REASON_LABELS,
    empty_details,
    render_summary,
    save_runtime,
    write_summary,
    validate_authentication_result,
)
from scripts.public_report import iso_date, validate_details, validate_snapshot
from scripts.state import manual_renewal_ignores_expiry


class RunCancelled(Exception):
    """运行被取消。"""


def run(platform: str, context: Dict[str, Any], check_only: bool = False) -> int:
    """启动一个账号的续期，并仅发布经过校验的报告。"""
    if platform not in {"hax", "woiden"}:
        raise ValueError("invalid_platform")
    if (
        os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("GITHUB_REF") != "refs/heads/master"
    ):
        raise ValueError("invalid_branch")
    root = Path(__file__).resolve().parent.parent
    output = root / f"{platform}-time-value.txt"
    if not check_only:
        output.unlink(missing_ok=True)
    fields = runtime_account(platform, dict(os.environ))
    index = fields.index
    context["index"] = index
    timeout = _seconds("PUBLIC_RUN_TIMEOUT_SECONDS", 3000, 3300)
    heartbeat = _seconds("PUBLIC_HEARTBEAT_SECONDS", 15, 60)
    register_masks(
        fields.masks()
        + [os.environ.get(name, "") for name in ("PROXY_CONTENT", "TWOCAPTCHA_API_KEY")]
    )
    previous_handlers = {
        sig: signal.signal(sig, _cancel) for sig in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        with tempfile.TemporaryDirectory(prefix="h-autorenew-") as temporary:
            private = Path(temporary).resolve()
            report = private / "report.json"
            environment = _worker_environment(fields, private, report)
            environment["HAX_AUTH_CHECK_ONLY"] = "1" if check_only else "0"
            hax_local_force = (
                platform == "hax"
                and os.environ.get("GITHUB_ACTIONS") != "true"
                and environment.get("HAX_FORCE_RENEWAL") == "1"
            )
            environment.pop("HAX_FORCE_RENEWAL", None)
            environment.pop("WOIDEN_FORCE_RENEWAL", None)
            if not check_only and (hax_local_force or manual_renewal_ignores_expiry(os.environ)):
                environment[f"{platform.upper()}_FORCE_RENEWAL"] = "1"
            binary = root / "sing-box"
            if binary.is_file():
                (private / "sing-box").symlink_to(binary)
            started = time.monotonic()
            print(f"platform={platform} account={index} stage=preparing", flush=True)
            if platform == "hax":
                environment["HAX_OPERATION_DEADLINE"] = str(
                    started + max(0, timeout - min(60, timeout / 10))
                )
            with subprocess.Popen(
                [sys.executable, "-u", str(root / f"{platform}-renew.py")],
                cwd=private,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            ) as worker:
                try:
                    while worker.poll() is None:
                        remaining = timeout - (time.monotonic() - started)
                        if remaining <= 0:
                            raise TimeoutError("run_timeout")
                        try:
                            worker.wait(timeout=min(heartbeat, remaining))
                        except subprocess.TimeoutExpired:
                            stage, _, details = (
                                _read_report(report)
                                if report.exists() and not check_only
                                else ("preparing", None, empty_details())
                            )
                            context.update(stage=stage, details=details)
                            elapsed = int(time.monotonic() - started)
                            print(
                                f"platform={platform} account={index} stage={stage} elapsed_seconds={elapsed}",
                                flush=True,
                            )
                finally:
                    _stop_worker(worker)
                if check_only:
                    if report.stat().st_size > 1024:
                        raise ValueError("invalid_auth_report")
                    value = validate_authentication_result(
                        json.loads(report.read_text())
                    )
                    code = (
                        0
                        if value["status"] == "valid" and worker.returncode == 0
                        else 1
                    )
                    context.update(
                        result="failed" if code else "complete",
                        reason=value["reason"]
                        if value["reason"] != "none" or not code
                        else "worker_error",
                        details={
                            **empty_details(),
                            **({"checks": value["checks"]} if value["checks"] else {}),
                        },
                    )
                    for name, check in value["checks"].items():
                        print(
                            f"platform=hax account={index} stage=diagnostic "
                            f"check={name} step={check['step']} result={check['result']}",
                            flush=True,
                        )
                    print(
                        f"platform=hax account={index} result={'failed' if code else 'authenticated'} mode=check-auth",
                        flush=True,
                    )
                    return code
                stage, summary, details = _read_report(report)
                if stage != "complete" or summary is None:
                    raise ValueError("missing_result")
                context.update(
                    stage=stage,
                    summary=summary,
                    details=details,
                    reason=details["reason"],
                )
                if platform == "hax":
                    for name, check in details.get("checks", {}).items():
                        print(
                            f"platform=hax account={index} stage=diagnostic "
                            f"check={name} step={check['step']} result={check['result']}",
                            flush=True,
                        )
                if platform == "hax" and details.get("navigation"):
                    navigation = details["navigation"][-1]
                    print(
                        f"platform=hax account={index} stage=navigation "
                        + " ".join(
                            f"{key}={str(value).lower()}"
                            for key, value in navigation.items()
                        ),
                        flush=True,
                    )
                value = private / f"{platform}-time-value.txt"
                if value.exists():
                    if value.stat().st_size > 256:
                        raise ValueError("invalid_state")
                    text = value.read_text(encoding="utf-8").strip()
                    key, state = text.split(",")
                    if key != state_key(fields.phone) or (
                        state != "NOVPS" and iso_date(state) is None
                    ):
                        raise ValueError("invalid_state")
                    if (state == "NOVPS") != (summary["no_vps"] == 1):
                        raise ValueError("inconsistent_state")
                    output.write_text(f"{key},{state}\n", encoding="utf-8")
                elapsed = int(time.monotonic() - started)
                counters = " ".join(f"{key}={count}" for key, count in summary.items())
                print(
                    f"platform={platform} account={index} stage=complete {counters} elapsed_seconds={elapsed}",
                    flush=True,
                )
                if worker.returncode and not (
                    summary["failed"] or summary["unverified"]
                ):
                    print("result=failed category=worker_error", flush=True)
                    context["reason"] = "worker_error"
                code = (
                    1
                    if worker.returncode or summary["failed"] or summary["unverified"]
                    else 0
                )
                context["result"] = "failed" if code else "complete"
                return code
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)


def main() -> int:
    """错误路径也仅输出固定错误类别。"""
    root = Path(__file__).resolve().parent.parent
    check_only = sys.argv[1:] == ["hax", "--check-auth"]
    platform = (
        sys.argv[1]
        if len(sys.argv) in {2, 3} and sys.argv[1] in {"hax", "woiden"}
        else "hax"
    )
    raw_index = os.environ.get("BATCH_ACCOUNT_INDEX", "1")
    index = (
        int(raw_index)
        if raw_index.isascii() and raw_index.isdigit() and len(raw_index) <= 3
        else 0
    )
    index = index if 1 <= index <= 256 else 0
    # 统一校验器限制这些混合类型字段，写文件前再次拒绝额外字段和自由文本。
    context: Dict[str, Any] = dict(
        version=1,
        platform=platform,
        index=index,
        result="failed",
        stage="preparing",
        reason="none",
        elapsed_seconds=0,
        summary=None,
        details=empty_details(),
    )
    started = time.monotonic()
    try:
        if check_only:
            (root / AUTHENTICATION_FILE).unlink(missing_ok=True)
        else:
            (root / RUNTIME_FILE).unlink(missing_ok=True)
        if len(sys.argv) != 2 and not check_only:
            raise ValueError("invalid_arguments")
        return run(sys.argv[1], context, check_only=check_only)
    except RunCancelled:
        context.update(result="cancelled", reason="cancelled")
        print("result=failed category=cancelled", flush=True)
        return 130
    except TimeoutError:
        context["reason"] = "timeout"
        print("result=failed category=timeout", flush=True)
        return 1
    except ValueError:
        context["summary"] = None
        context["reason"] = "invalid_configuration_or_report"
        print("result=failed category=invalid_configuration_or_report", flush=True)
        return 1
    except Exception:
        context["reason"] = "worker_error"
        print("result=failed category=worker_error", flush=True)
        return 1
    finally:
        context["elapsed_seconds"] = min(int(time.monotonic() - started), 86400)
        try:
            if check_only:
                authentication = validate_authentication_result(
                    dict(
                        version=1,
                        status="valid" if context["result"] == "complete" else "failed",
                        reason=context["reason"]
                        if context["reason"] != "none"
                        or context["result"] == "complete"
                        else "worker_error",
                        checks=context["details"].get("checks", {}),
                    )
                )
                destination = root / AUTHENTICATION_FILE
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(json.dumps(authentication) + "\n")
                status = (
                    "✅ 两类登录态检查通过"
                    if context["result"] == "complete"
                    else "❌ 登录态检查失败"
                )
                reason = REASON_LABELS.get(context["reason"], "未能完成检查")
                write_summary(
                    f"### Hax · 登录态检查\n\n**{status}**\n\n本次未提交续期，未改变到期日缓存。\n\n{reason}\n"
                )
            else:
                save_runtime(root, context)
            if not check_only and os.environ.get("PUBLIC_SUMMARY_DEFERRED") != "1":
                write_summary(
                    render_summary(
                        platform,
                        context["index"],
                        {
                            "RENEW_OUTCOME": "success"
                            if context["result"] == "complete"
                            else "failure"
                        },
                        root,
                    )
                )
        except Exception:
            print("result=failed category=summary_unavailable", flush=True)


def _seconds(name: str, default: int, maximum: int) -> int:
    value = int(os.environ.get(name, str(default)))
    if not 1 <= value <= maximum:
        raise ValueError("invalid_timeout")
    return value


def _worker_environment(fields: Account, private: Path, report: Path) -> Dict[str, str]:
    environment = dict(os.environ)
    for key in list(environment):
        if key.startswith(
            ("GITHUB_", "ACTIONS_", "RENEWAL_", "TELEGRAM_", "TG_")
        ) or key in {
            "GH_TOKEN",
            "PUBLIC_REPORT_FILE",
            "BATCH",
            "HAX_ACCOUNT_FILE",
        }:
            environment.pop(key)
    environment.update(
        {
            "BATCH_ACCOUNT_INDEX": "1",
            "PUBLIC_REPORT_FILE": str(report),
            "SCREENSHOT_DIR": str(private / "screenshots"),
            "TMPDIR": str(private),
            "TMP": str(private),
            "TEMP": str(private),
            "DEBUG_FLAG": "0",
            "PYTHONUNBUFFERED": "1",
        }
    )
    if fields.web is not None:
        config = private / "account.json"
        save_private_json(config, dict(version=1, accounts=[fields.web]))
        environment.update(
            HAX_ACCOUNT_FILE=str(config),
            TELEGRAM_BOT_TOKEN=fields.token,
            TELEGRAM_CHAT_ID=fields.chat,
        )
    else:
        environment["BATCH"] = ",".join(fields.legacy or [])
    return environment


def _read_report(path: Path) -> tuple[str, Dict[str, int] | None, Dict[str, Any]]:
    if not path.is_file() or path.stat().st_size > 4096:
        raise ValueError("invalid_report")
    value = json.loads(path.read_text(encoding="utf-8"))
    stage, summary = validate_snapshot(value)
    return stage, summary, validate_details(value["details"])


def _cancel(signum: int, frame: object) -> None:
    raise RunCancelled()


def _descendants(parent: int) -> List[int]:
    # warp-cli 会另建会话，不能仅依赖主进程组清理。
    result = subprocess.run(
        ["ps", "-eo", "pid=,ppid="], capture_output=True, text=True, timeout=3
    )
    pairs = [
        tuple(map(int, line.split()))
        for line in result.stdout.splitlines()
        if len(line.split()) == 2
    ]
    found = {parent}
    while True:
        children = {pid for pid, ppid in pairs if ppid in found}
        if children <= found:
            return list(found - {parent})
        found.update(children)


def _signal_process(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            subprocess.run(
                ["sudo", "-n", "kill", f"-{sig}", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
            )


def _stop_worker(worker: subprocess.Popen) -> None:
    try:
        descendants = _descendants(worker.pid)
    except Exception:
        descendants = []
    for pid in descendants:
        _signal_process(pid, signal.SIGTERM)
    try:
        os.killpg(worker.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        worker.wait(timeout=3)
    except subprocess.TimeoutExpired:
        worker.kill()
        worker.wait(timeout=3)
    for pid in descendants:
        _signal_process(pid, signal.SIGKILL)
    try:
        os.killpg(worker.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


if __name__ == "__main__":
    sys.exit(main())

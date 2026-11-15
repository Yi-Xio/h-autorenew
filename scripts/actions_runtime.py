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

from scripts.accounts import register_masks, selected_account, state_key
from scripts.job_summary import (
    RUNTIME_FILE,
    empty_details,
    render_summary,
    save_runtime,
    write_summary,
)
from scripts.public_report import iso_date, validate_details, validate_snapshot


class RunCancelled(Exception):
    """运行被取消。"""


def run(platform: str, context: Dict[str, Any]) -> int:
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
    output.unlink(missing_ok=True)
    index, fields = selected_account(dict(os.environ))
    timeout = _seconds("PUBLIC_RUN_TIMEOUT_SECONDS", 3000, 3300)
    heartbeat = _seconds("PUBLIC_HEARTBEAT_SECONDS", 15, 60)
    register_masks(
        fields
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
            binary = root / "sing-box"
            if binary.is_file():
                (private / "sing-box").symlink_to(binary)
            started = time.monotonic()
            print(f"platform={platform} account={index} stage=preparing", flush=True)
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
                                if report.exists()
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
                stage, summary, details = _read_report(report)
                if stage != "complete" or summary is None:
                    raise ValueError("missing_result")
                context.update(
                    stage=stage,
                    summary=summary,
                    details=details,
                    reason=details["reason"],
                )
                value = private / f"{platform}-time-value.txt"
                if value.exists():
                    if value.stat().st_size > 256:
                        raise ValueError("invalid_state")
                    text = value.read_text(encoding="utf-8").strip()
                    key, state = text.split(",")
                    if key != state_key(fields[0]) or (
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
    platform = (
        sys.argv[1]
        if len(sys.argv) == 2 and sys.argv[1] in {"hax", "woiden"}
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
        (root / RUNTIME_FILE).unlink(missing_ok=True)
        if len(sys.argv) != 2:
            raise ValueError("invalid_arguments")
        return run(sys.argv[1], context)
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
            save_runtime(root, context)
            if os.environ.get("PUBLIC_SUMMARY_DEFERRED") != "1":
                write_summary(
                    render_summary(
                        platform,
                        index,
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


def _worker_environment(
    fields: List[str], private: Path, report: Path
) -> Dict[str, str]:
    environment = dict(os.environ)
    for key in list(environment):
        if key.startswith(("GITHUB_", "ACTIONS_")) or key in {
            "GH_TOKEN",
            "PUBLIC_REPORT_FILE",
        }:
            environment.pop(key)
    environment.update(
        {
            "BATCH": ",".join(fields),
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

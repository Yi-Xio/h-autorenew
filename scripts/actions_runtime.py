"""公开 Actions 的续期入口，原脚本输出只进入空设备。"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List

from scripts.accounts import register_masks, selected_account, state_key
from scripts.public_report import iso_date, validate_snapshot


class RunCancelled(Exception):
    """运行被取消。"""


def run(platform: str) -> int:
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
                            stage, _ = (
                                _read_report(report)
                                if report.exists()
                                else ("preparing", None)
                            )
                            elapsed = int(time.monotonic() - started)
                            print(
                                f"platform={platform} account={index} stage={stage} elapsed_seconds={elapsed}",
                                flush=True,
                            )
                finally:
                    _stop_worker(worker)
                stage, summary = _read_report(report)
                if stage != "complete" or summary is None:
                    raise ValueError("missing_result")
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
                _step_summary(platform, index, summary)
                if worker.returncode and not (
                    summary["failed"] or summary["unverified"]
                ):
                    print("result=failed category=worker_error", flush=True)
                return (
                    1
                    if worker.returncode or summary["failed"] or summary["unverified"]
                    else 0
                )
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)


def main() -> int:
    """错误路径也仅输出固定错误类别。"""
    try:
        if len(sys.argv) != 2:
            raise ValueError("invalid_arguments")
        return run(sys.argv[1])
    except RunCancelled:
        print("result=failed category=cancelled", flush=True)
        return 130
    except TimeoutError:
        print("result=failed category=timeout", flush=True)
        return 1
    except ValueError:
        print("result=failed category=invalid_configuration_or_report", flush=True)
        return 1
    except Exception:
        print("result=failed category=worker_error", flush=True)
        return 1


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


def _read_report(path: Path) -> tuple[str, Dict[str, int] | None]:
    if not path.is_file() or path.stat().st_size > 4096:
        raise ValueError("invalid_report")
    return validate_snapshot(json.loads(path.read_text(encoding="utf-8")))


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


def _step_summary(platform: str, index: int, summary: Dict[str, int]) -> None:
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with Path(target).open("a", encoding="utf-8") as output:
            output.write(f"### {platform} account {index}\n\n")
            output.write("| Result | Count |\n| --- | --- |\n")
            for key, count in summary.items():
                output.write(f"| {key} | {count} |\n")


if __name__ == "__main__":
    sys.exit(main())

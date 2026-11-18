"""从 Shell 入口验证取消后的容器停止与私有目录解锁。"""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.regression.fixtures import PROJECT, environment


class EntrypointTests(unittest.TestCase):
    def test_live_help_exits_without_project_configuration(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hax-live-help-") as temporary:
            script = Path(temporary) / "run.sh"
            shutil.copy2(PROJECT / "tests/live/run.sh", script)
            for args in ([], ["--help"], ["renew", "--help"], ["check-auth", "--help"]):
                with self.subTest(args=args):
                    result = subprocess.run(
                        ["bash", str(script), *args], capture_output=True, text=True, check=True
                    )
                    self.assertIn("run.sh renew [--force]", result.stdout)
                    self.assertIn("run.sh check-auth", result.stdout)
                    self.assertEqual(result.stderr, "")

    def test_cancel_stops_current_container_and_releases_private_lock(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hax-entrypoint-test-") as temporary:
            root = Path(temporary)
            for relative in (
                "tests/docker/common.sh",
                "tests/live/run.sh",
                ".gitignore",
                ".dockerignore",
            ):
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(PROJECT / relative, target)
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            executable = root / "bin/docker"
            executable.parent.mkdir()
            executable.write_text(
                f"#!{sys.executable}\n"
                + """import os, signal, sys, time
from pathlib import Path
root = Path(os.environ["EXAMPLE_DOCKER_ROOT"])
arguments = sys.argv[1:]
with (root / "calls.txt").open("a") as output:
    output.write(" ".join(arguments) + "\\n")
if arguments[0] == "run":
    print(f"{os.getuid()}:{os.getgid()}")
elif arguments[0] == "compose" and "run" in arguments:
    (root / "docker-pid").write_text(str(os.getpid()))
    (root / "started").touch()
    time.sleep(60)
elif arguments[0] == "stop":
    (root / "stopped").touch()
    os.kill(int((root / "docker-pid").read_text()), signal.SIGTERM)
"""
            )
            executable.chmod(0o700)
            env = environment()
            env.update(
                PATH=str(executable.parent) + os.pathsep + env["PATH"],
                EXAMPLE_DOCKER_ROOT=str(root),
            )
            process = subprocess.Popen(
                ["bash", str(root / "tests/live/run.sh"), "renew"],
                cwd=root,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + 5
                while not (root / "started").exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue((root / "started").exists())
                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=3)
                self.assertEqual(process.returncode, 130, stdout + stderr)
                self.assertTrue((root / "stopped").exists())
                self.assertFalse((root / ".local/state/operation.lock").exists())
                calls = (root / "calls.txt").read_text()
                self.assertIn("live live renew", calls)
                self.assertNotIn("regression", calls)
            finally:
                if process.poll() is None:
                    process.kill()
                pid_file = root / "docker-pid"
                if pid_file.exists():
                    try:
                        os.kill(int(pid_file.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.communicate(timeout=3)

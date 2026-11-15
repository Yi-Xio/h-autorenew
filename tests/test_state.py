"""用本地 Git 远端和假凭据验证公开状态及并发回写。"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.accounts import public_account_id, state_key
from scripts.state import (
    read_observed_state,
    read_repository_state,
    save_repository_state,
)
from tests.test_runtime import ACCOUNT, SECOND, environment


class StateTests(unittest.TestCase):
    """所有提交只进入临时本地仓库，不访问 GitHub。"""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="hax-test-git-state-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.remote = self.root / "remote.git"
        self.fields = ACCOUNT.split(",")
        self.other = SECOND.split(",")
        self.account = public_account_id("hax", self.fields)
        self.env = environment()
        self.env.update(
            GIT_AUTHOR_NAME="Test",
            GIT_AUTHOR_EMAIL="test@example.invalid",
            GIT_COMMITTER_NAME="Test",
            GIT_COMMITTER_EMAIL="test@example.invalid",
        )
        real_git = shutil.which("git")
        assert real_git
        self.real_git = real_git

    def git(self, *arguments: str, root: Path | None = None) -> str:
        """只在测试仓库执行 Git，测试进程自身也不转发命令输出。"""
        result = subprocess.run(
            [self.real_git, *arguments],
            cwd=root or self.work,
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
        return result.stdout.strip()

    def repository(self) -> None:
        """创建含原版短索引的远端，用来验证首次公开回写。"""
        self.git("init", "--bare", str(self.remote))
        self.git("init", "-b", "master")
        (self.work / "hax-time.txt").write_text("deadbeef,2026-12-01\n")
        (self.work / "kept.txt").write_text("original\n")
        self.git("add", "hax-time.txt", "kept.txt")
        self.git("commit", "-m", "seed")
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "origin", "master")

    def observe(
        self, state: str = "2026-12-01", platform: str = "hax", account: str = ACCOUNT
    ) -> None:
        """模拟受控执行入口的内部交接文件。"""
        (self.work / f"{platform}-time-value.txt").write_text(
            f"{state_key(account.split(',')[0])},{state}\n"
        )

    def save(self, platform: str = "hax", account: str = ACCOUNT) -> str:
        with patch.dict(os.environ, self.env, clear=True):
            return save_repository_state(self.work, platform, account.split(","))

    def test_public_identity_separates_accounts_platforms_and_session_rotation(
        self,
    ) -> None:
        self.assertRegex(self.account, r"^[a-f0-9]{64}$")
        self.assertNotEqual(self.account, public_account_id("hax", self.other))
        self.assertNotEqual(self.account, public_account_id("woiden", self.fields))
        self.assertNotEqual(
            self.account,
            public_account_id("hax", [*self.fields[:-1], "ROTATED_SESSION"]),
        )
        changed = [*self.fields]
        changed[1] = "CHANGED_BOT"
        self.assertEqual(self.account, public_account_id("hax", changed))
        for value in self.fields + [state_key(self.fields[0])]:
            self.assertNotIn(value, self.account)

    def test_legacy_records_are_not_used_and_invalid_public_rows_are_rejected(
        self,
    ) -> None:
        path = self.root / "state.txt"
        self.assertEqual(read_repository_state(path), {})
        path.write_text(f"deadbeef,2099-12-31\n{self.account},NOVPS\n")
        self.assertEqual(read_repository_state(path), {self.account: "NOVPS"})
        for body in (
            "FAKE_SESSION",
            f"{self.account},bad-date",
            f"{self.account},NOVPS\n{self.account},NOVPS",
            "x" * 65537,
        ):
            with self.subTest(body=body):
                path.write_text(body)
                with self.assertRaises(ValueError):
                    read_repository_state(path)

    def test_observation_must_match_current_account_and_have_valid_state(self) -> None:
        path = self.work / "hax-time-value.txt"
        self.assertIsNone(read_observed_state(path, self.fields))
        for body in (
            "FAKE_SESSION",
            f"{state_key(self.other[0])},NOVPS",
            f"{state_key(self.fields[0])},2026-02-30",
            "x" * 257,
        ):
            with self.subTest(body=body):
                path.write_text(body)
                with self.assertRaises(ValueError):
                    read_observed_state(path, self.fields)
        self.observe("NOVPS")
        self.assertEqual(read_observed_state(path, self.fields), "NOVPS")

    def test_missing_or_wrong_result_cannot_write_and_same_result_adds_no_commit(
        self,
    ) -> None:
        self.assertEqual(self.save(), "no_observed_state")
        self.repository()
        before = self.git("rev-parse", "master", root=self.remote)
        self.observe(account=SECOND)
        with self.assertRaises(ValueError):
            self.save()
        self.assertEqual(self.git("rev-parse", "master", root=self.remote), before)
        self.observe()
        self.assertEqual(self.save(), "saved")
        saved = self.git("rev-parse", "master", root=self.remote)
        self.assertEqual(self.save(), "unchanged")
        self.assertEqual(self.git("rev-parse", "master", root=self.remote), saved)

    def test_writes_only_opaque_rows_and_keeps_working_files_and_other_accounts(
        self,
    ) -> None:
        self.repository()
        (self.work / "kept.txt").write_text("uncommitted user changes\n")
        for platform in ("hax", "woiden"):
            for account in (ACCOUNT, SECOND):
                self.observe(platform=platform, account=account)
                self.assertEqual(self.save(platform, account), "saved")
            text = self.git("show", f"master:{platform}-time.txt", root=self.remote)
            for value in (
                self.fields
                + self.other
                + [state_key(self.fields[0]), state_key(self.other[0]), "deadbeef"]
            ):
                self.assertNotIn(value, text)
            self.assertEqual(len(text.splitlines()), 2)
        self.assertEqual(
            (self.work / "kept.txt").read_text(), "uncommitted user changes\n"
        )
        self.assertEqual(
            self.git("rev-list", "--count", "master", root=self.remote), "5"
        )
        self.assertEqual(self.git("rev-list", "--count", "master"), "1")

    def test_concurrent_remote_update_is_preserved_after_retry(self) -> None:
        self.repository()
        competitor = self.root / "competitor"
        self.git("clone", "--branch", "master", str(self.remote), str(competitor))
        other_id = public_account_id("hax", self.other)
        (competitor / "hax-time.txt").write_text(f"{other_id},2026-12-02\n")
        self.git("add", "hax-time.txt", root=competitor)
        self.git("commit", "-m", "concurrent update", root=competitor)
        binary = self.root / "bin"
        binary.mkdir()
        marker = self.root / "injected"
        wrapper = binary / "git"
        wrapper.write_text(
            f"#!{sys.executable}\nimport os, subprocess, sys\nfrom pathlib import Path\n"
            f"real = {self.real_git!r}\nmarker = Path({str(marker)!r})\n"
            "if sys.argv[1] == 'push' and not marker.exists():\n"
            "    marker.touch()\n"
            f"    subprocess.run([real, 'push', 'origin', 'master'], cwd={str(competitor)!r}, check=True, capture_output=True)\n"
            "os.execv(real, [real, *sys.argv[1:]])\n"
        )
        wrapper.chmod(0o700)
        self.env["PATH"] = str(binary) + os.pathsep + self.env["PATH"]
        self.observe()
        self.assertEqual(self.save(), "saved")
        text = self.git("show", "master:hax-time.txt", root=self.remote)
        self.assertIn(f"{other_id},2026-12-02", text)
        self.assertIn(f"{self.account},2026-12-01", text)
        self.assertTrue(marker.exists())

    def test_rejected_push_keeps_remote_state(self) -> None:
        self.repository()
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho FAKE_SESSION >&2\nexit 1\n")
        hook.chmod(0o700)
        before = self.git("rev-parse", "master", root=self.remote)
        self.observe()
        with self.assertRaises(ValueError):
            self.save()
        self.assertEqual(self.git("rev-parse", "master", root=self.remote), before)


if __name__ == "__main__":
    unittest.main()

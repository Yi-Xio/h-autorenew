"""验证真实 Git 排除、工作树与私有目录保护。"""

import subprocess
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.web_sessions import SessionError, load_private_json, save_private_json


class PrivateDirectoryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="hax-private-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self.git("init", "--quiet")
        (self.repository / ".gitignore").write_text("/.local/\n")
        (self.repository / ".dockerignore").write_text(".local/\n.git/\n")

    def git(self, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(self.repository), *args],
            check=True,
            capture_output=True,
            timeout=10,
        )

    def test_only_excluded_untracked_directory_accepts_credentials(self) -> None:
        path = self.repository / ".local/secrets/A001/telegram.json"
        save_private_json(path, {"example": "private"})
        self.assertEqual(load_private_json(path), {"example": "private"})
        for directory in (path.parent, path.parents[1], path.parents[2]):
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        for other in (self.repository / "token.json", path.parent / "../other.json"):
            with self.assertRaises(SessionError):
                save_private_json(other, "example")
        self.git("add", "--force", str(path))
        with self.assertRaises(SessionError):
            save_private_json(path.with_name("next.json"), "example")
        with patch.dict(
            os.environ, {"GIT_INDEX_FILE": str(self.root / "another-index")}
        ):
            with self.assertRaises(SessionError):
                save_private_json(path.with_name("other.json"), "example")

    def test_missing_exclusions_and_reinclude_rules_fail_before_writing(self) -> None:
        path = self.repository / ".local/secrets/session.json"
        for git_rules, docker_rules in (
            ("", ".local/\n"),
            ("/.local/\n", ""),
            ("/.local/\n", ".local/\n!.local/secrets/\n"),
        ):
            (self.repository / ".gitignore").write_text(git_rules)
            (self.repository / ".dockerignore").write_text(docker_rules)
            with self.assertRaises(SessionError):
                save_private_json(path, "example")
            self.assertFalse(path.exists())

    def test_parent_alias_and_public_private_root_are_rejected(self) -> None:
        local = self.repository / ".local"
        local.mkdir(mode=0o755)
        local.chmod(0o755)
        with self.assertRaises(SessionError):
            save_private_json(local / "secrets/session.json", "example")
        local.chmod(0o700)
        alias = self.root / "alias"
        alias.symlink_to(local, target_is_directory=True)
        with self.assertRaises(SessionError):
            save_private_json(alias / "secrets/session.json", "example")

    def test_git_worktree_file_keeps_same_protection(self) -> None:
        self.git(
            "-c",
            "user.name=Example",
            "-c",
            "user.email=example@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "test baseline",
        )
        worktree = self.root / "worktree"
        self.git("worktree", "add", "--detach", "--quiet", str(worktree))
        (worktree / ".gitignore").write_text("/.local/\n")
        (worktree / ".dockerignore").write_text(".local/\n")
        path = worktree / ".local/secrets/session.json"
        save_private_json(path, "example")
        self.assertEqual(load_private_json(path), "example")

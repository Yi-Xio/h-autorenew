"""从公开接口验证缓存归属、数据校验和本轮结果保存。"""

import json
import tempfile
import unittest
from pathlib import Path

from scripts.accounts import public_account_id, state_key
from scripts.state import prepare_cached_state, read_cached_state
from tests.regression.fixtures import ACCOUNT, SECOND


class StateTests(unittest.TestCase):
    """仅使用临时文件和假凭据，不执行 Git 或远程请求。"""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="hax-test-state-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "cached.json"
        self.observed = self.root / "observed.txt"
        self.fields = ACCOUNT.split(",")
        self.account = public_account_id("hax", self.fields)

    def observe(self, state: str = "2026-12-01") -> None:
        """写入第一组执行入口约定的本账号结果格式。"""
        self.observed.write_text(f"{state_key(self.fields[0])},{state}\n")

    def test_identity_separates_platforms_and_accounts_without_plain_credentials(
        self,
    ) -> None:
        self.assertRegex(self.account, r"^[a-f0-9]{64}$")
        self.assertNotEqual(self.account, public_account_id("hax", SECOND.split(",")))
        self.assertNotEqual(self.account, public_account_id("woiden", self.fields))
        rotated = [*self.fields[:-1], "ROTATED_FAKE_SESSION"]
        self.assertNotEqual(self.account, public_account_id("hax", rotated))
        changed_bot = [*self.fields]
        changed_bot[1] = "OTHER_FAKE_BOT"
        self.assertEqual(self.account, public_account_id("hax", changed_bot))
        for secret in self.fields + [state_key(self.fields[0])]:
            self.assertNotIn(secret, self.account)

    def test_round_trip_only_contains_validated_state_and_opaque_identity(self) -> None:
        for state in ("2026-12-01", "NOVPS"):
            with self.subTest(state=state):
                self.observe(state)
                self.assertEqual(
                    prepare_cached_state(self.path, self.observed, "hax", self.fields),
                    "changed",
                )
                self.assertEqual(
                    read_cached_state(self.path, "hax", self.account), state
                )
                text = self.path.read_text()
                self.assertEqual(
                    set(json.loads(text)), {"version", "platform", "account", "state"}
                )
                for secret in self.fields + [state_key(self.fields[0])]:
                    self.assertNotIn(secret, text)

    def test_unchanged_or_missing_result_keeps_existing_snapshot(self) -> None:
        self.observe()
        prepare_cached_state(self.path, self.observed, "hax", self.fields)
        original = self.path.read_bytes()
        self.assertEqual(
            prepare_cached_state(self.path, self.observed, "hax", self.fields),
            "unchanged",
        )
        self.assertEqual(self.path.read_bytes(), original)
        self.observed.unlink()
        self.assertEqual(
            prepare_cached_state(self.path, self.observed, "hax", self.fields),
            "no_observed_state",
        )
        self.assertEqual(self.path.read_bytes(), original)

    def test_wrong_or_invalid_observed_result_cannot_replace_snapshot(self) -> None:
        self.observe()
        prepare_cached_state(self.path, self.observed, "hax", self.fields)
        original = self.path.read_bytes()
        for text in (
            "FAKE_SESSION",
            f"{state_key(SECOND.split(',')[0])},2026-12-02",
            f"{state_key(self.fields[0])},bad-date",
            "x" * 257,
        ):
            with self.subTest(text=text):
                self.observed.write_text(text)
                with self.assertRaises(ValueError):
                    prepare_cached_state(self.path, self.observed, "hax", self.fields)
                self.assertEqual(self.path.read_bytes(), original)

    def test_bad_cache_schema_or_binding_is_rejected(self) -> None:
        valid = dict(
            version=1, platform="hax", account=self.account, state="2026-12-01"
        )
        bad = [
            [],
            {**valid, "version": True},
            {**valid, "version": 2},
            {**valid, "platform": "woiden"},
            {**valid, "account": public_account_id("hax", SECOND.split(","))},
            {**valid, "state": "2026-02-30"},
            {**valid, "state": False},
            {**valid, "extra": "FAKE_SESSION"},
        ]
        for value in bad:
            with self.subTest(value=value):
                self.path.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    read_cached_state(self.path, "hax", self.account)
        self.path.write_text("x" * 4097)
        with self.assertRaises(ValueError):
            read_cached_state(self.path, "hax", self.account)

    def test_valid_observation_rebuilds_bad_or_untrusted_cache(self) -> None:
        self.observe()
        self.path.write_text("FAKE_SESSION,bad-date")
        self.assertEqual(
            prepare_cached_state(self.path, self.observed, "hax", self.fields),
            "changed",
        )
        self.assertEqual(
            read_cached_state(self.path, "hax", self.account), "2026-12-01"
        )
        self.assertEqual(
            prepare_cached_state(
                self.path, self.observed, "hax", self.fields, restored=False
            ),
            "changed",
        )


if __name__ == "__main__":
    unittest.main()

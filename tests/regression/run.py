"""运行完整示例回归；没有发现用例或跳过浏览器场景均不通过。"""

import sys
import unittest
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    suite = unittest.defaultTestLoader.discover(
        str(root / "tests/regression"), top_level_dir=str(root)
    )
    if suite.countTestCases() == 0:
        print("未发现回归测试。", file=sys.stderr)
        return 1
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() and not result.skipped else 1


if __name__ == "__main__":
    sys.exit(main())

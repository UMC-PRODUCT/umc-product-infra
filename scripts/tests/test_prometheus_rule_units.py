#!/usr/bin/env python3
"""UMC Prometheus 알림이 의도한 입력에서만 발화하는지 promtool 단위 테스트로 확인한다."""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RULE_TESTS = sorted((ROOT / "observability/rules/tests").glob("*.test.yaml"))


class PrometheusRuleUnitTest(unittest.TestCase):
    def test_promtool_rule_units(self) -> None:
        self.assertTrue(RULE_TESTS, "promtool rule test 입력이 없습니다")
        promtool = shutil.which("promtool")
        if promtool is None:
            self.skipTest("promtool not installed; Prometheus rule unit tests skipped")
        result = subprocess.run(
            [promtool, "test", "rules", *map(str, RULE_TESTS)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()

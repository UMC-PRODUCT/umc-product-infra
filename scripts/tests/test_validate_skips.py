from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

SKIPS = Path(__file__).resolve().parents[1] / "validate-skips.sh"


class ValidationSkipTests(unittest.TestCase):
    def run_checks(self, *, strict: bool, missing: tuple[str, ...]) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "umc-fake-present"
            fake.write_text("#!/bin/sh\n")
            fake.chmod(0o755)
            checks = ["if tool_available umc-fake-present 'present check'; then echo 'present check ran'; fi"]
            checks += [f"if tool_available umc-fake-{tool} '{tool} check'; then exit 9; fi" for tool in missing]
            script = "\n".join(["set -euo pipefail", f"source '{SKIPS}'", *checks, "finish_validation"])
            environment = {"PATH": f"{directory}:/usr/bin:/bin"}
            if strict:
                environment["VALIDATE_STRICT"] = "1"
            return subprocess.run(["bash", "-c", script], env=environment, capture_output=True, text=True)

    def test_도구가_모두_있으면_skip_없이_통과(self) -> None:
        for strict in (False, True):
            with self.subTest(strict=strict):
                result = self.run_checks(strict=strict, missing=())
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.splitlines(), ["present check ran", "umc-infra validation passed"])

    def test_건너뛴_검사를_마지막_줄에_모두_표시(self) -> None:
        result = self.run_checks(strict=False, missing=("cfn", "docker"))
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertIn("umc-fake-cfn not installed; cfn check skipped", lines)
        self.assertEqual(lines[-1], "umc-infra validation passed (skipped: cfn check, docker check)")

    def test_확인_명령이_실패하면_skip으로_기록(self) -> None:
        result = subprocess.run(
            ["bash", "-c", f"set -euo pipefail; source '{SKIPS}'; "
             "if tool_available bash 'docker check' false; then exit 9; fi; finish_validation"],
            env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[-1], "umc-infra validation passed (skipped: docker check)")

    def test_strict_모드는_skip이_하나라도_있으면_실패(self) -> None:
        result = self.run_checks(strict=True, missing=("promtool",))
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("validation passed", result.stdout)
        self.assertIn("skipped: promtool check", result.stderr)

    def test_validate_sh의_선택_도구는_모두_skip_기록을_거침(self) -> None:
        script = (SKIPS.parent / "validate.sh").read_text(encoding="utf-8")
        self.assertEqual(script.count("command -v"), 1, "필수 helm 외 선택 도구는 tool_available로만 확인")
        self.assertTrue(script.rstrip().endswith("finish_validation"))


if __name__ == "__main__":
    unittest.main()

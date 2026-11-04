from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
SKIPS = SCRIPTS / "validate-skips.sh"
BASH = shutil.which("bash") or "/bin/bash"


def run_bash(script: str, *, path: str, strict: bool) -> subprocess.CompletedProcess:
    environment = {"PATH": path}
    if strict:
        environment["VALIDATE_STRICT"] = "1"
    return subprocess.run(
        [BASH, "-c", "\n".join(["set -euo pipefail", f"source '{SKIPS}'", script, "finish_validation"])],
        cwd=SCRIPTS.parent, env=environment, capture_output=True, text=True, check=False,
    )


class ValidationSkipTests(unittest.TestCase):
    def test_전제가_모두_통과하면_skip_없이_통과(self) -> None:
        for strict in (False, True):
            with self.subTest(strict=strict):
                result = run_bash(
                    "if optional_check 'present check' true; then echo 'present check ran'; fi",
                    path="/usr/bin:/bin", strict=strict,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.splitlines(), ["present check ran", "umc-infra validation passed"])

    def test_건너뛴_검사를_마지막_줄에_모두_표시(self) -> None:
        result = run_bash(
            "if optional_check 'cfn check' command -v umc-fake-cfn; then exit 9; fi\n"
            "if optional_check 'docker check' false; then exit 9; fi",
            path="/usr/bin:/bin", strict=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertIn("cfn check skipped: 'command -v umc-fake-cfn' failed", lines)
        self.assertEqual(lines[-1], "umc-infra validation passed (skipped: cfn check, docker check)")

    def test_strict_모드는_skip이_하나라도_있으면_실패(self) -> None:
        result = run_bash("if optional_check 'promtool check' false; then exit 9; fi", path="/usr/bin:/bin", strict=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("validation passed", result.stdout)
        self.assertIn("skipped: promtool check", result.stderr)

    # validate.sh 전체는 chart 렌더와 네트워크가 필요하므로 선택 도구 구간만 잘라 도구 없는 PATH로 실행한다.
    def test_validate_sh의_선택_도구_skip은_모두_요약에_남음(self) -> None:
        script = (SCRIPTS / "validate.sh").read_text(encoding="utf-8")
        start = script.index("for script in scripts/*.sh; do")
        end = script.rindex("finish_validation")
        section = script[start:end]
        with tempfile.TemporaryDirectory() as directory:
            os.symlink(BASH, Path(directory) / "bash")
            section = f"validation_dir='{directory}'\n{section}"
            result = run_bash(section, path=directory, strict=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = result.stdout.splitlines()
            reported = [line for line in lines[:-1] if "skipped" in line]
            self.assertTrue(lines[-1].startswith("umc-infra validation passed (skipped: "), lines[-1])
            summary = lines[-1].removeprefix("umc-infra validation passed (skipped: ").removesuffix(")").split(", ")
            self.assertEqual(len(reported), len(summary), "요약에 빠진 skip 출력이 있음")
            self.assertGreaterEqual(len(summary), 6)
            strict = run_bash(section, path=directory, strict=True)
            self.assertNotEqual(strict.returncode, 0)
            self.assertNotIn("validation passed", strict.stdout)


if __name__ == "__main__":
    unittest.main()

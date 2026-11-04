from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from validate_argocd import check_helm_version


class HelmVersionTests(unittest.TestCase):
    def test_고정_버전과_같으면_경고_없음(self) -> None:
        for strict in (False, True):
            self.assertIsNone(check_helm_version("v4.2.4+g1234567", "v4.2.4", strict=strict))

    def test_같은_minor의_patch_차이는_로컬에서_경고로_허용(self) -> None:
        warning = check_helm_version("v4.2.3+g43e8b7f", "v4.2.4", strict=False)
        self.assertIsNotNone(warning)
        self.assertIn("v4.2.4", warning or "")

    def test_strict_모드는_patch까지_일치해야_함(self) -> None:
        with self.assertRaises(SystemExit):
            check_helm_version("v4.2.3+g43e8b7f", "v4.2.4", strict=True)

    def test_minor가_다르면_필요한_버전을_안내하고_실패(self) -> None:
        for actual in ("v4.1.9+gabc", "v4.3.0", "v3.2.4", "unknown"):
            with self.subTest(actual=actual), self.assertRaises(SystemExit) as raised:
                check_helm_version(actual, "v4.2.4", strict=False)
            self.assertIn("install Helm v4.2.4", str(raised.exception))


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Tests for repository-wide contract validation helpers."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from validate_contracts import git_visible_files


class GitVisibleFilesTest(unittest.TestCase):
    def test_excludes_ignored_files_but_keeps_untracked_visible_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)

            (root / ".gitignore").write_text("*.dump\n", encoding="utf-8")
            (root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
            (root / "untracked.txt").write_text("untracked\n", encoding="utf-8")
            (root / "tracked.dump").write_text("tracked despite ignore\n", encoding="utf-8")
            (root / "database.dump").write_text("ignored\n", encoding="utf-8")
            (root / "name with spaces.txt").write_text("visible\n", encoding="utf-8")
            subprocess.run(["git", "add", "-f", "tracked.dump"], cwd=root, check=True)
            subprocess.run(
                ["git", "add", ".gitignore", "tracked.txt"],
                cwd=root,
                check=True,
            )

            visible = {path.relative_to(root) for path in git_visible_files(root)}

            self.assertEqual(
                visible,
                {
                    Path(".gitignore"),
                    Path("name with spaces.txt"),
                    Path("tracked.dump"),
                    Path("tracked.txt"),
                    Path("untracked.txt"),
                },
            )


if __name__ == "__main__":
    unittest.main()

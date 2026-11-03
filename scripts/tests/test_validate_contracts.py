#!/usr/bin/env python3
"""Tests for repository-wide contract validation helpers."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from validate_contracts import (
    git_visible_files,
    validate_preview_budget_arithmetic,
    validate_repository_identity,
)

REPOSITORY_ROOT = SCRIPTS_DIR.parent


# 오래된 루트 runbooks가 없어도 새 checkout이 통과하고 docs/runbooks 누락은 실패해야 한다.
class RepositoryLayoutTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for path in (
            "ansible", "argocd/applications", "bootstrap",
            "charts/umc-product-server", "charts/umc-secrets", "cloud/aws",
            "manifests/cluster", "manifests/cert-manager",
            "manifests/postgres/prod", "manifests/postgres/dev",
            "manifests/postgres/preview", "manifests/observability",
            "observability", "docs/runbooks",
        ):
            (self.root / path).mkdir(parents=True)

    def validate(self) -> None:
        with (
            patch("validate_contracts.ROOT", self.root),
            patch("validate_contracts.git_visible_files", return_value=[]),
        ):
            validate_repository_identity()

    def test_accepts_runbooks_under_docs_without_legacy_directory(self) -> None:
        self.validate()

    def test_legacy_directory_does_not_replace_docs_runbooks(self) -> None:
        (self.root / "docs/runbooks").rmdir()
        (self.root / "runbooks").mkdir()

        with self.assertRaisesRegex(SystemExit, "missing directory: docs/runbooks"):
            self.validate()


# ignored 비추적 파일만 제외한다. 이미 추적한 파일과 공개된 새 파일은 계속 검증해야 한다.
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


def pod(kind: str, *containers: tuple[str, str, str]) -> dict:
    spec = {
        "containers": [
            {"resources": {"requests": {"cpu": cpu, "memory": memory}, "limits": {"memory": limit}}}
            for cpu, memory, limit in containers
        ]
    }
    return {"kind": kind, "spec": {"template": {"spec": spec}}}


# PR 하나의 render(앱+Mailpit, createdb, dropdb)를 두 슬롯만큼 더한 값이 quota와 맞아야 한다.
class PreviewBudgetArithmeticTest(unittest.TestCase):
    PR_RESOURCES = [
        pod("Deployment", ("250m", "2Gi", "3Gi"), ("20m", "64Mi", "128Mi")),
        pod("Job", ("100m", "128Mi", "512Mi")),
        pod("Job", ("50m", "64Mi", "128Mi")),
    ]

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for relative in (
            "manifests/cluster/preview-resourcequota.yaml",
            "manifests/postgres/preview/statefulset.yaml",
            "manifests/postgres/preview/orphan-database-check-cronjob.yaml",
        ):
            (self.root / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(REPOSITORY_ROOT / relative, self.root / relative)

    def validate(self, resources: list[dict]) -> None:
        with patch("validate_contracts.ROOT", self.root):
            validate_preview_budget_arithmetic(resources)

    def test_accepts_two_slots_with_two_dropdb_jobs(self) -> None:
        self.validate(self.PR_RESOURCES)

    def test_rejects_budget_that_fits_only_one_dropdb_job(self) -> None:
        quota = self.root / "manifests/cluster/preview-resourcequota.yaml"
        text = quota.read_text(encoding="utf-8")
        for current, single_dropdb in (
            ("requests.cpu: 1110m", "requests.cpu: 1060m"),
            ("requests.memory: 6688Mi", "requests.memory: 6624Mi"),
            ("limits.memory: 9792Mi", "limits.memory: 9664Mi"),
        ):
            text = text.replace(current, single_dropdb)
        quota.write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, r"quota \[1060, 6624, 9664\]"):
            self.validate(self.PR_RESOURCES)

    def test_rejects_pr_persistent_volume_claim(self) -> None:
        with self.assertRaisesRegex(SystemExit, "only the shared PostgreSQL PVC"):
            self.validate(self.PR_RESOURCES + [{"kind": "PersistentVolumeClaim"}])


if __name__ == "__main__":
    unittest.main()

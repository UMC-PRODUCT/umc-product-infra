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
    validate_app_storage_contract,
    validate_cloudformation_tags,
    validate_repository_identity,
    validate_ses_contract,
)

REPO_ROOT = SCRIPTS_DIR.parent


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


# 실제 template 사본 한 줄을 바꿔 복구성·메일 인증·태그 계약이 깨지면 검증이 실패해야 한다.
class CloudFormationContractTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        shutil.copytree(REPO_ROOT / "cloud" / "aws", self.root / "cloud" / "aws")
        chart = self.root / "charts" / "umc-product-server"
        chart.mkdir(parents=True)
        for environment in ("prod", "dev", "preview"):
            name = f"values-{environment}.yaml"
            shutil.copy(REPO_ROOT / "charts" / "umc-product-server" / name, chart / name)

    def edit(self, template: str, old: str, new: str) -> None:
        path = self.root / "cloud" / "aws" / template
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count(old), 1, f"fixture anchor not unique in {template}")
        path.write_text(text.replace(old, new), encoding="utf-8")

    def validate(self, validator) -> None:
        with patch("validate_contracts.ROOT", self.root):
            validator()

    def test_repository_templates_pass(self) -> None:
        for validator in (
            validate_app_storage_contract,
            validate_ses_contract,
            validate_cloudformation_tags,
        ):
            self.validate(validator)

    def test_rejects_unversioned_app_storage(self) -> None:
        self.edit(
            "app-storage-s3.yaml",
            "        - Status: Enabled\n",
            "        - Status: Suspended\n",
        )
        with self.assertRaisesRegex(SystemExit, "versioning must be enabled"):
            self.validate(validate_app_storage_contract)

    def test_rejects_app_permission_to_delete_versions(self) -> None:
        self.edit(
            "app-storage-s3.yaml",
            "                  - s3:DeleteObject\n",
            "                  - s3:DeleteObject\n                  - s3:DeleteObjectVersion\n",
        )
        with self.assertRaisesRegex(SystemExit, "must not delete object versions"):
            self.validate(validate_app_storage_contract)

    def test_rejects_changed_noncurrent_version_retention(self) -> None:
        self.edit("app-storage-s3.yaml", "NoncurrentDays: 30", "NoncurrentDays: 3650")
        with self.assertRaisesRegex(SystemExit, "noncurrent versions"):
            self.validate(validate_app_storage_contract)

    def test_rejects_missing_dmarc_record(self) -> None:
        self.edit("ses-email.yaml", "  DmarcDnsRecord:\n", "  DmarcDnsRecordRemoved:\n")
        with self.assertRaisesRegex(SystemExit, "SES Route53 record set"):
            self.validate(validate_ses_contract)

    def test_rejects_enforcing_dmarc_policy_change(self) -> None:
        self.edit("ses-email.yaml", "v=DMARC1; p=none;", "v=DMARC1; p=reject;")
        with self.assertRaisesRegex(SystemExit, "SES DMARC TXT record"):
            self.validate(validate_ses_contract)

    def test_rejects_untagged_iam_role(self) -> None:
        self.edit(
            "external-secrets-iam.yaml",
            "          Value: external-secrets-prod\n",
            "          Value: \"\"\n",
        )
        with self.assertRaisesRegex(SystemExit, "external-secrets-iam.yaml:ProdRole"):
            self.validate(validate_cloudformation_tags)


if __name__ == "__main__":
    unittest.main()

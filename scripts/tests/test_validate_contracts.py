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
    external_dns_hosts,
    git_visible_files,
    validate_app_storage_contract,
    validate_cloudformation_tags,
    validate_external_dns_record_names,
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

    def edit(self, template: str, old: str, new: str, count: int = 1) -> None:
        path = self.root / "cloud" / "aws" / template
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count(old), count, f"fixture anchor count in {template}")
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

    def test_rejects_app_wildcard_action(self) -> None:
        self.edit(
            "app-storage-s3.yaml",
            "                  - s3:DeleteObject\n",
            "                  - s3:DeleteObject*\n",
        )
        with self.assertRaisesRegex(SystemExit, "wildcard actions"):
            self.validate(validate_app_storage_contract)

    def test_reports_unreviewed_app_policy_change_as_snapshot_drift(self) -> None:
        self.edit(
            "app-storage-s3.yaml",
            "                  - s3:DeleteObject\n",
            "                  - s3:DeleteObject\n                  - s3:GetObjectTagging\n",
        )
        with self.assertRaisesRegex(SystemExit, "differs from the reviewed copy"):
            self.validate(validate_app_storage_contract)

    def test_rejects_changed_noncurrent_version_retention(self) -> None:
        self.edit("app-storage-s3.yaml", "NoncurrentDays: 30", "NoncurrentDays: 3650")
        with self.assertRaisesRegex(SystemExit, "noncurrent versions"):
            self.validate(validate_app_storage_contract)

    def test_rejects_missing_dmarc_record(self) -> None:
        self.edit("ses-email.yaml", "  DmarcDnsRecord:\n", "  DmarcDnsRecordRemoved:\n")
        with self.assertRaisesRegex(SystemExit, "SES Route53 record set"):
            self.validate(validate_ses_contract)

    def test_accepts_enforcing_dmarc_policies(self) -> None:
        previous = "none"
        for policy in ("quarantine", "reject"):
            self.edit("ses-email.yaml", f"v=DMARC1; p={previous}", f"v=DMARC1; p={policy}", count=2)
            self.validate(validate_ses_contract)
            previous = policy

    def test_rejects_unknown_dmarc_policy(self) -> None:
        self.edit("ses-email.yaml", "v=DMARC1; p=none", "v=DMARC1; p=monitor", count=2)
        with self.assertRaisesRegex(SystemExit, "SES DMARC TXT record"):
            self.validate(validate_ses_contract)

    def test_rejects_dmarc_reports_to_feedback_mailbox(self) -> None:
        self.edit(
            "ses-email.yaml",
            "rua=mailto:${DmarcReportEmailAddress}",
            "rua=mailto:${FeedbackNotificationEmailAddress}",
        )
        with self.assertRaisesRegex(SystemExit, "rua only from DmarcReportEmailAddress"):
            self.validate(validate_ses_contract)

    def test_rejects_default_dmarc_report_address(self) -> None:
        self.edit(
            "ses-email.yaml",
            "  DmarcReportEmailAddress:\n    Type: String\n    Default: \"\"\n",
            "  DmarcReportEmailAddress:\n    Type: String\n    Default: ops@example.com\n",
        )
        with self.assertRaisesRegex(SystemExit, "empty by default"):
            self.validate(validate_ses_contract)

    def test_rejects_untagged_iam_role(self) -> None:
        self.edit(
            "external-secrets-iam.yaml",
            "          Value: external-secrets-prod\n",
            "          Value: \"\"\n",
        )
        with self.assertRaisesRegex(SystemExit, "external-secrets-iam.yaml:ProdRole"):
            self.validate(validate_cloudformation_tags)

    def test_rejects_iam_role_without_environment_tag(self) -> None:
        self.edit(
            "external-secrets-iam.yaml",
            "        - Key: environment\n          Value: dev\n",
            "",
        )
        with self.assertRaisesRegex(SystemExit, "external-secrets-iam.yaml:DevRole"):
            self.validate(validate_cloudformation_tags)


ZONE = "university.neordinary.com"
ALLOWED_PATTERNS = [
    f"api.{ZONE}",
    f"api-pr-*.{ZONE}",
    f"mail-pr-*.{ZONE}",
    f"_external-dns.a-api.{ZONE}",
    f"_external-dns.a-api-pr-*.{ZONE}",
    f"_external-dns.a-mail-pr-*.{ZONE}",
]
RENDERED_HOSTS = {f"api.{ZONE}", f"api-pr-42.{ZONE}", f"mail-pr-42.{ZONE}"}


# ExternalDNS가 만들 A·소유권 TXT 이름은 모두 IAM이 허용하고, IAM 패턴은 렌더된 이름보다 넓지 않아야 한다.
class ExternalDnsRecordNameTest(unittest.TestCase):
    def validate(self, patterns: list[str], hosts: set[str]) -> None:
        validate_external_dns_record_names(
            patterns, hosts, txt_prefix="_external-dns.", record_types=["A", "TXT"]
        )

    def test_accepts_rendered_hosts_with_ownership_txt(self) -> None:
        self.validate(ALLOWED_PATTERNS, RENDERED_HOSTS)

    def test_rejects_new_host_without_iam_permission(self) -> None:
        with self.assertRaisesRegex(SystemExit, "does not allow record name: .*docs-pr-42"):
            self.validate(ALLOWED_PATTERNS, RENDERED_HOSTS | {f"docs-pr-42.{ZONE}"})

    def test_rejects_host_without_ownership_txt_permission(self) -> None:
        patterns = [p for p in ALLOWED_PATTERNS if p != f"_external-dns.a-mail-pr-*.{ZONE}"]
        with self.assertRaisesRegex(
            SystemExit, f"record name: _external-dns.a-mail-pr-42.{ZONE}"
        ):
            self.validate(patterns, RENDERED_HOSTS)

    def test_rejects_zone_wide_wildcard(self) -> None:
        for broad in (f"*.{ZONE}", f"_external-dns.a-*.{ZONE}", f"mail-*.{ZONE}"):
            with self.subTest(broad=broad), self.assertRaisesRegex(
                SystemExit, "broader than a rendered record"
            ):
                self.validate([*ALLOWED_PATTERNS, broad], RENDERED_HOSTS)

    def test_rejects_pattern_without_rendered_host(self) -> None:
        with self.assertRaisesRegex(SystemExit, f"no longer used: grafana.{ZONE}"):
            self.validate([*ALLOWED_PATTERNS, f"grafana.{ZONE}"], RENDERED_HOSTS)

    def test_collects_only_opted_in_ingress_hosts(self) -> None:
        opted_in = {"external-dns.kubernetes.io/managed-by": "umc-infra"}
        documents = [
            {
                "kind": "Ingress",
                "metadata": {
                    "annotations": {
                        **opted_in,
                        "external-dns.kubernetes.io/hostname": f"extra.{ZONE}, alias.{ZONE}",
                    }
                },
                "spec": {
                    "rules": [{"host": f"api.{ZONE}"}],
                    "tls": [{"hosts": [f"tls-only.{ZONE}"]}],
                },
            },
            {
                "kind": "Ingress",
                "metadata": {"annotations": {}},
                "spec": {"rules": [{"host": f"unmanaged.{ZONE}"}]},
            },
            {"kind": "Service", "metadata": {"annotations": opted_in}},
        ]

        self.assertEqual(
            external_dns_hosts(documents),
            {f"api.{ZONE}", f"tls-only.{ZONE}", f"extra.{ZONE}", f"alias.{ZONE}"},
        )


if __name__ == "__main__":
    unittest.main()

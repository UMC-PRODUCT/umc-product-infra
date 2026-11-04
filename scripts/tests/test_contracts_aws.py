#!/usr/bin/env python3
"""Tests for S3 app storage, SES, and CloudFormation tag contracts."""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from contracts.aws import (  # noqa: E402
    validate_app_storage_contract,
    validate_cloudformation_tags,
    validate_ses_contract,
)

REPO_ROOT = SCRIPTS_DIR.parent


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
        with patch("contracts.common.ROOT", self.root):
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


if __name__ == "__main__":
    unittest.main()

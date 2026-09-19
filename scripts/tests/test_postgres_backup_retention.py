#!/usr/bin/env python3
"""Regression checks for production PostgreSQL backup retention and access."""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


class PostgresBackupRetentionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # BaseLoader accepts CloudFormation tags without evaluating them; scalars stay strings.
        template = yaml.load(
            (ROOT / "cloud/aws/postgres-backup-s3.yaml").read_text(),
            Loader=yaml.BaseLoader,
        )
        cls.resources = template["Resources"]
        cls.bucket = cls.resources["BackupBucket"]
        cls.properties = cls.bucket["Properties"]
        cls.rules = cls.properties["LifecycleConfiguration"]["Rules"]

    def test_retention_is_ten_days_for_production_only(self) -> None:
        self.assertEqual(self.rules[0], {
            "Id": "expire-production-postgres-backups",
            "Status": "Enabled",
            "Prefix": "postgres/prod/",
            "ExpirationInDays": "10",
            "NoncurrentVersionExpiration": {"NoncurrentDays": "1"},
            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": "1"},
        })

    def test_expired_delete_markers_use_a_separate_production_rule(self) -> None:
        self.assertEqual(len(self.rules), 2)
        self.assertEqual(self.rules[1], {
            "Id": "remove-expired-production-postgres-delete-markers",
            "Status": "Enabled",
            "Prefix": "postgres/prod/",
            "ExpiredObjectDeleteMarker": "true",
        })

    def test_bucket_preserves_versioning_encryption_and_stack_retention(self) -> None:
        self.assertEqual(self.bucket["DeletionPolicy"], "Retain")
        self.assertEqual(self.bucket["UpdateReplacePolicy"], "Retain")
        self.assertEqual(self.properties["VersioningConfiguration"], {"Status": "Enabled"})
        self.assertEqual(self.properties["BucketEncryption"], {
            "ServerSideEncryptionConfiguration": [{
                "ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"},
            }],
        })

    def test_writer_cannot_read_or_delete_backup_objects(self) -> None:
        policies = self.resources["BackupWriter"]["Properties"]["Policies"]
        self.assertEqual(len(policies), 1)
        self.assertEqual(policies[0]["PolicyDocument"]["Statement"], [{
            "Sid": "ReadBucketLocation",
            "Effect": "Allow",
            "Action": "s3:GetBucketLocation",
            "Resource": "BackupBucket.Arn",
        }, {
            "Sid": "UploadBackupObjects",
            "Effect": "Allow",
            "Action": ["s3:PutObject", "s3:AbortMultipartUpload"],
            "Resource": "${BackupBucket.Arn}/postgres/prod/*",
        }])


if __name__ == "__main__":
    unittest.main()

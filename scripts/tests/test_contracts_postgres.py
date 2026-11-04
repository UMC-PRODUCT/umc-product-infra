#!/usr/bin/env python3
"""Tests for PostgreSQL backup role contracts."""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from contracts.postgres import validate_backup_role

REPO_ROOT = SCRIPTS_DIR.parent


# backup role의 추가 권한·약해진 최종 검사, backup Pod의 관리자·다른 role 접속, lock 무한 대기, exporter를 막는 wave는 실패해야 한다.
class BackupRoleContractTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.prod = Path(directory.name) / "prod"
        shutil.copytree(REPO_ROOT / "manifests/postgres/prod", self.prod)

    def replace(self, filename: str, old: str, new: str) -> None:
        path = self.prod / filename
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count(old), 1, old)
        path.write_text(text.replace(old, new), encoding="utf-8")

    def assert_rejected(self, message: str) -> None:
        with self.assertRaisesRegex(SystemExit, message):
            validate_backup_role(self.prod)

    def test_accepts_repository_manifests(self) -> None:
        validate_backup_role(self.prod)

    def add_grant(self, grant: str) -> None:
        self.replace(
            "postgres-backup-role-job.yaml",
            "GRANT pg_read_all_data TO umc_product_backup;",
            f"GRANT pg_read_all_data TO umc_product_backup;\n              {grant}",
        )

    def test_rejects_extra_privilege_for_backup_role(self) -> None:
        self.add_grant("GRANT pg_write_all_data TO umc_product_backup;")
        self.assert_rejected("backup role must be read-only")

    def test_rejects_quoted_lowercase_grant_for_backup_role(self) -> None:
        self.add_grant('grant INSERT ON ALL TABLES IN SCHEMA public to "umc_product_backup";')
        self.assert_rejected("backup role must be read-only")

    def test_rejects_weakened_final_state_check(self) -> None:
        self.replace("postgres-backup-role-job.yaml", " AND rolconnlimit = 2", "")
        self.assert_rejected("pinned final-state check")

    def test_rejects_change_after_final_state_check(self) -> None:
        self.replace(
            "postgres-backup-role-job.yaml",
            "$role_check$;\n              COMMIT;",
            "$role_check$;\n              ALTER ROLE umc_product_backup SUPERUSER;\n              COMMIT;",
        )
        self.assert_rejected("pinned final-state check")

    def test_rejects_commit_before_final_state_check(self) -> None:
        self.replace(
            "postgres-backup-role-job.yaml",
            "GRANT pg_read_all_data TO umc_product_backup;",
            "GRANT pg_read_all_data TO umc_product_backup;\n              COMMIT;\n              BEGIN;",
        )
        self.assert_rejected("must not commit before the final-state check")

    def test_rejects_role_job_that_can_block_the_exporter(self) -> None:
        self.replace(
            "postgres-backup-role-job.yaml",
            'argocd.argoproj.io/sync-wave: "5"',
            'argocd.argoproj.io/sync-wave: "4"',
        )
        self.assert_rejected("must sync after postgres-exporter")

    def test_rejects_renamed_exporter_with_message(self) -> None:
        (self.prod / "postgres-exporter.yaml").rename(self.prod / "exporter.yaml")
        self.assert_rejected("postgres-exporter.yaml is missing")

    def test_rejects_backup_dump_with_admin_secret(self) -> None:
        self.replace(
            "backup-cronjob.yaml",
            "secretKeyRef: { name: postgres-backup, key: POSTGRES_BACKUP_PASSWORD }",
            "secretKeyRef: { name: postgres-secrets, key: POSTGRES_PASSWORD }",
        )
        self.assert_rejected("read-only backup role")

    def test_rejects_backup_dump_as_admin_user(self) -> None:
        self.replace("backup-cronjob.yaml", "value: umc_product_backup", "value: postgres")
        self.assert_rejected("read-only backup role")

    def test_rejects_backup_dump_with_other_role_secret(self) -> None:
        self.replace(
            "backup-cronjob.yaml",
            "secretKeyRef: { name: postgres-backup, key: POSTGRES_BACKUP_PASSWORD }",
            "secretKeyRef: { name: app-db, key: DATABASE_PASSWORD }",
        )
        self.assert_rejected("read-only backup role")

    def test_rejects_admin_secret_elsewhere_in_backup_pod(self) -> None:
        self.replace(
            "backup-cronjob.yaml",
            '                - name: AWS_PAGER\n                  value: ""\n',
            '                - name: AWS_PAGER\n                  value: ""\n'
            "              envFrom:\n                - secretRef: { name: postgres-secrets }\n",
        )
        self.assert_rejected("read-only backup role")

    def test_rejects_backup_dump_without_lock_wait_timeout(self) -> None:
        self.replace(
            "backup-cronjob.yaml",
            'pg_dump -Fc --lock-wait-timeout=5min -f "$backup_file"',
            'pg_dump -Fc -f "$backup_file"',
        )
        self.assert_rejected("instead of waiting forever for table locks")

    def test_rejects_backup_without_dump_container(self) -> None:
        self.replace("backup-cronjob.yaml", "- name: dump\n", "- name: pg-dump\n")
        self.assert_rejected("init container named dump")


if __name__ == "__main__":
    unittest.main()

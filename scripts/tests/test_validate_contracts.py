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
    pod_resource_totals,
    validate_backup_role,
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


def container(cpu: str, memory: str, limit: str, **fields: str) -> dict:
    return {
        "resources": {"requests": {"cpu": cpu, "memory": memory}, "limits": {"memory": limit}},
        **fields,
    }


def pod(kind: str, *containers: tuple[str, str, str], init: tuple = ()) -> dict:
    spec = {"containers": [container(*item) for item in containers]}
    if init:
        spec["initContainers"] = [container(*item) for item in init]
    return {"kind": kind, "spec": {"template": {"spec": spec}}}


# quota는 init container를 일반 container에 더하지 않고 자원별 최댓값으로 센다.
class PodResourceTotalsTest(unittest.TestCase):
    def totals(self, item: dict) -> tuple[int, int, int]:
        return pod_resource_totals(item["spec"]["template"]["spec"])

    def test_small_init_container_does_not_add_to_running_containers(self) -> None:
        deployment = pod(
            "Deployment",
            ("250m", "2Gi", "3Gi"),
            ("20m", "64Mi", "128Mi"),
            init=[("20m", "64Mi", "128Mi")],
        )
        self.assertEqual(self.totals(deployment), (270, 2112, 3200))

    def test_each_resource_takes_the_larger_of_init_and_running_sum(self) -> None:
        deployment = pod(
            "Deployment",
            ("250m", "2Gi", "3Gi"),
            ("20m", "64Mi", "128Mi"),
            init=[("300m", "64Mi", "128Mi"), ("10m", "4Gi", "128Mi")],
        )
        self.assertEqual(self.totals(deployment), (300, 4096, 3200))

    def test_rejects_sidecar_init_container(self) -> None:
        spec = pod("Deployment", ("250m", "2Gi", "3Gi"))["spec"]["template"]["spec"]
        spec["initContainers"] = [container("20m", "64Mi", "128Mi", restartPolicy="Always")]
        with self.assertRaisesRegex(SystemExit, "sidecar init containers"):
            pod_resource_totals(spec)


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

    def test_init_container_smaller_than_app_keeps_the_same_budget(self) -> None:
        with_init = [
            pod(
                "Deployment",
                ("250m", "2Gi", "3Gi"),
                ("20m", "64Mi", "128Mi"),
                init=[("20m", "64Mi", "128Mi")],
            ),
            *self.PR_RESOURCES[1:],
        ]
        self.validate(with_init)

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


# backup role의 추가 권한·약해진 최종 검사, backup Pod의 관리자 Secret, exporter를 막는 wave는 실패해야 한다.
class BackupRoleContractTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.prod = Path(directory.name) / "prod"
        shutil.copytree(REPOSITORY_ROOT / "manifests/postgres/prod", self.prod)

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


if __name__ == "__main__":
    unittest.main()

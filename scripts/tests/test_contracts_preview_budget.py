#!/usr/bin/env python3
"""Tests for Preview resource quota budget contracts."""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from contracts.preview_budget import (
    pod_resource_totals,
    validate_preview_budget_arithmetic,
)

REPO_ROOT = SCRIPTS_DIR.parent


def container(cpu: str, memory: str, limit: str, **fields: str) -> dict:
    return {
        "resources": {"requests": {"cpu": cpu, "memory": memory}, "limits": {"memory": limit}},
        **fields,
    }


def pod(kind: str, *containers: tuple[str, str, str], init: Sequence[tuple[str, str, str]] = ()) -> dict:
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
            "manifests/postgres/preview/postgres-exporter.yaml",
        ):
            (self.root / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(REPO_ROOT / relative, self.root / relative)

    def validate(self, resources: list[dict]) -> None:
        with patch("contracts.common.ROOT", self.root):
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
            ("requests.cpu: 1120m", "requests.cpu: 1070m"),
            ("requests.memory: 6720Mi", "requests.memory: 6656Mi"),
            ("limits.memory: 9856Mi", "limits.memory: 9728Mi"),
        ):
            text = text.replace(current, single_dropdb)
        quota.write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, r"quota \[1070, 6656, 9728\]"):
            self.validate(self.PR_RESOURCES)

    def test_rejects_pr_persistent_volume_claim(self) -> None:
        with self.assertRaisesRegex(SystemExit, "only the shared PostgreSQL PVC"):
            self.validate(self.PR_RESOURCES + [{"kind": "PersistentVolumeClaim"}])


if __name__ == "__main__":
    unittest.main()

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

import yaml

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from validate_contracts import (
    git_visible_files,
    validate_edge_protection,
    validate_repository_identity,
)

CHART = SCRIPTS_DIR.parent / "charts" / "umc-product-server"


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


# 앱 Ingress에 edge 제한이 빠지거나 기준이 IP에서 벗어나면 실패하고, 환경별 한도 조정은 통과해야 한다.
@unittest.skipUnless(shutil.which("helm"), "helm not installed")
class EdgeProtectionTest(unittest.TestCase):
    def render(self, *extra: str) -> list[dict]:
        result = subprocess.run(
            [
                "helm", "template", "umc-product-server", str(CHART),
                "--namespace", "app",
                "-f", str(CHART / "values-prod.yaml"),
                *extra,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return [item for item in yaml.safe_load_all(result.stdout) if item]

    def named(self, resources: list[dict], name: str) -> dict:
        return next(
            item
            for item in resources
            if item["kind"] in {"Ingress", "Middleware"} and item["metadata"]["name"] == name
        )

    def middlewares(self, resources: list[dict], name: str) -> dict:
        return self.named(resources, name)["metadata"]["annotations"]

    def test_accepts_prod_render(self) -> None:
        validate_edge_protection(self.render(), "prod")

    def test_accepts_environment_override_of_limits(self) -> None:
        validate_edge_protection(
            self.render(
                "--set", "edgeProtection.rateLimit.average=50",
                "--set", "edgeProtection.rateLimit.burst=5000",
                "--set", "edgeProtection.inFlight.amount=600",
                "--set", "edgeProtection.webSocketInFlight.amount=3000",
            ),
            "prod",
        )

    def test_rejects_main_ingress_without_in_flight_limit(self) -> None:
        resources = self.render()
        self.middlewares(resources, "umc-product-server")[
            "traefik.ingress.kubernetes.io/router.middlewares"
        ] = "app-umc-product-server-rate-limit@kubernetescrd"
        with self.assertRaisesRegex(SystemExit, "umc-product-server must apply edge limits first"):
            validate_edge_protection(resources, "prod")

    def test_rejects_basic_auth_before_edge_limits(self) -> None:
        resources = self.render()
        annotations = self.middlewares(resources, "umc-product-server-docs")
        refs = annotations["traefik.ingress.kubernetes.io/router.middlewares"].split(",")
        annotations["traefik.ingress.kubernetes.io/router.middlewares"] = ",".join(refs[::-1])
        with self.assertRaisesRegex(SystemExit, "docs must apply edge limits first"):
            validate_edge_protection(resources, "prod")

    def test_rejects_in_flight_limit_grouped_by_host(self) -> None:
        resources = self.render()
        del self.named(resources, "umc-product-server-in-flight")["spec"]["inFlightReq"]["sourceCriterion"]
        with self.assertRaisesRegex(SystemExit, "umc-product-server-in-flight must exist"):
            validate_edge_protection(resources, "prod")

    def test_rejects_forwarded_header_ip_strategy(self) -> None:
        resources = self.render()
        self.named(resources, "umc-product-server-rate-limit")["spec"]["rateLimit"][
            "sourceCriterion"
        ] = {"ipStrategy": {"depth": 1}}
        with self.assertRaisesRegex(SystemExit, "rate-limit must count per client remote address"):
            validate_edge_protection(resources, "prod")

    def test_rejects_unlimited_websocket_in_flight(self) -> None:
        resources = self.render()
        self.named(resources, "umc-product-server-ws-in-flight")["spec"]["inFlightReq"]["amount"] = 0
        with self.assertRaisesRegex(SystemExit, "ws-in-flight inFlightReq.amount must be a positive integer"):
            validate_edge_protection(resources, "prod")

    def test_rejects_websocket_route_without_websocket_in_flight(self) -> None:
        resources = self.render()
        self.middlewares(resources, "umc-product-server-ws")[
            "traefik.ingress.kubernetes.io/router.middlewares"
        ] = "app-umc-product-server-rate-limit@kubernetescrd"
        with self.assertRaisesRegex(SystemExit, "WebSocket route must use rate-limit and the WebSocket in-flight limit"):
            validate_edge_protection(resources, "prod")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Regression checks for AppProject whitelists against rendered Application resources."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from argocd_project_kinds import CHART_RENDERS, Project, rejected_kinds, validation_errors


def resource(api_version: str, kind: str, name: str = "x", namespace: str | None = None, **spec: object) -> dict:
    metadata = {"name": name} | ({"namespace": namespace} if namespace else {})
    return {"apiVersion": api_version, "kind": kind, "metadata": metadata, "spec": spec}


# 실제 projects.yaml에 대해 chart 렌더를 바꿔 넣고, whitelist 밖 kind와 argocd 침범을 거부하는지 확인한다.
class ValidationErrorsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.output = Path(self.directory.name)
        self.renders = {name: [] for names in CHART_RENDERS.values() for name in names}
        self.renders["prod.yaml"] = [resource("apps/v1", "Deployment"),
                                     resource("traefik.io/v1alpha1", "Middleware")]
        self.renders["secrets.yaml"] = [
            resource("external-secrets.io/v1", "SecretStore", "aws-secretsmanager", "argocd"),
            resource("external-secrets.io/v1", "ExternalSecret", "preview-github-token", "argocd",
                     target={"name": "preview-github-token"}),
        ]

    def tearDown(self) -> None:
        self.directory.cleanup()

    def errors(self) -> list[str]:
        for name, documents in self.renders.items():
            (self.output / name).write_text(yaml.safe_dump_all(documents), encoding="utf-8")
        return validation_errors(self.output)

    def test_reviewed_renders_are_accepted(self) -> None:
        self.assertEqual(self.errors(), [])

    def test_unlisted_namespaced_kind_is_rejected(self) -> None:
        self.renders["prod.yaml"].append(resource("v1", "Secret"))
        self.assertEqual(self.errors(), [
            "prod: namespace kind core/Secret from umc-product-server is not whitelisted",
        ])

    def test_cluster_kind_is_checked_against_cluster_whitelist(self) -> None:
        self.renders["preview.yaml"].append(resource("rbac.authorization.k8s.io/v1", "ClusterRole"))
        self.assertEqual(self.errors(), [
            "preview: cluster kind rbac.authorization.k8s.io/ClusterRole "
            "from umc-product-server-preview is not whitelisted",
        ])

    def test_platform_cannot_target_other_argocd_secrets(self) -> None:
        self.renders["secrets.yaml"][1]["spec"]["target"]["name"] = "argocd-secret"
        self.assertEqual(self.errors(), [
            "platform: ExternalSecret/preview-github-token (target argocd-secret) may not be written into argocd",
        ])

    def test_missing_render_for_restricted_project_is_rejected(self) -> None:
        del self.renders["prod.yaml"]
        self.assertIn(f"missing render: umc-product-server: {self.output / 'prod.yaml'}", self.errors())


# Argo CD 의미 그대로 namespaced whitelist가 없으면 전부 허용, cluster whitelist는 없으면 전부 거부한다.
class ProjectSemanticsTest(unittest.TestCase):
    def test_absent_namespace_whitelist_permits_everything(self) -> None:
        project = Project("open", (), None)
        self.assertEqual(rejected_kinds({"open": {("", "Secret"): {"app"}}}, {"open": project}), [])

    def test_wildcard_entry_matches(self) -> None:
        project = Project("wild", (), (("traefik.io", "*"),))
        self.assertEqual(rejected_kinds({"wild": {("traefik.io", "Middleware"): {"app"}}}, {"wild": project}), [])

    def test_empty_cluster_whitelist_rejects_cluster_kind(self) -> None:
        project = Project("closed", (), (("", "Service"),))
        self.assertEqual(
            rejected_kinds({"closed": {("", "Namespace"): {"app"}}}, {"closed": project}),
            ["closed: cluster kind core/Namespace from app is not whitelisted"],
        )


if __name__ == "__main__":
    unittest.main()

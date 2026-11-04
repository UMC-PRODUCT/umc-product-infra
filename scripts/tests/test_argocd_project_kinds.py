#!/usr/bin/env python3
"""Regression checks for AppProject whitelists against rendered Application resources."""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argocd_project_kinds as kinds
from argocd_project_kinds import (
    CHART_RENDERS, PROJECTS_PATH, Project, applications, directory_renders, main, projects, validation_errors,
)


def resource(api_version: str, kind: str, name: str = "x", namespace: str | None = None, **spec: object) -> dict:
    metadata = {"name": name} | ({"namespace": namespace} if namespace else {})
    return {"apiVersion": api_version, "kind": kind, "metadata": metadata, "spec": spec}


# 실제 projects.yaml에 대해 chart 렌더를 바꿔 넣는다. 기본 렌더는 각 project의 namespaced whitelist kind를 하나씩 만든다.
class ValidationErrorsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.output = Path(self.directory.name)
        self.renders: dict[str, list[dict]] = {name: [] for names in CHART_RENDERS.values() for name in names}
        reviewed = projects()
        first_render: dict[str, str] = {}
        for app in applications(self.output):
            if app.name in CHART_RENDERS:
                first_render.setdefault(app.project, CHART_RENDERS[app.name][0])
        for project, render in first_render.items():
            for group, kind in reviewed[project].namespace_whitelist or ():
                self.renders[render].append(resource(f"{group}/v1" if group else "v1", kind))
        self.renders["secrets.yaml"] += [
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

    def widened_projects(self, project: str, entry: dict) -> Path:
        documents = list(yaml.safe_load_all(PROJECTS_PATH.read_text(encoding="utf-8")))
        for document in documents:
            if document["metadata"]["name"] == project:
                document["spec"]["namespaceResourceWhitelist"].append(entry)
        path = self.output / "projects.yaml"
        path.write_text(yaml.safe_dump_all(documents), encoding="utf-8")
        return path

    def test_reviewed_renders_are_accepted(self) -> None:
        self.assertEqual(self.errors(), [])

    def test_unlisted_namespaced_kind_is_rejected(self) -> None:
        self.renders["prod-desired-state.yaml"].append(resource("v1", "Secret"))
        self.assertEqual(self.errors(), [
            "prod: namespace kind core/Secret from umc-product-server is not whitelisted",
        ])

    def test_cluster_kind_is_checked_against_cluster_whitelist(self) -> None:
        self.renders["preview.yaml"].append(resource("rbac.authorization.k8s.io/v1", "ClusterRole"))
        self.assertEqual(self.errors(), [
            "preview: cluster kind rbac.authorization.k8s.io/ClusterRole "
            "from umc-product-server-preview is not whitelisted",
        ])

    def test_widening_a_whitelist_without_a_render_is_rejected(self) -> None:
        widened = self.widened_projects("prod", {"group": "", "kind": "Secret"})
        with mock.patch.object(kinds, "PROJECTS_PATH", widened):
            self.assertEqual(self.errors(), [
                "prod: namespace kind core/Secret is whitelisted but no Application renders it; "
                "remove it or justify it in NAMESPACE_WHITELIST_EXTRAS",
            ])

    def test_justified_extra_kind_is_accepted(self) -> None:
        widened = self.widened_projects("prod", {"group": "", "kind": "Secret"})
        extras = {"prod": {("", "Secret"): "test"}}
        with mock.patch.object(kinds, "PROJECTS_PATH", widened), \
                mock.patch.object(kinds, "NAMESPACE_WHITELIST_EXTRAS", extras):
            self.assertEqual(self.errors(), [])

    def test_platform_cannot_target_other_argocd_secrets(self) -> None:
        self.renders["secrets.yaml"][-1]["spec"]["target"]["name"] = "argocd-secret"
        self.assertEqual(self.errors(), [
            "platform: ExternalSecret/preview-github-token (target argocd-secret) may not be written into argocd",
        ])

    def test_platform_cannot_label_secret_for_argocd(self) -> None:
        self.renders["secrets.yaml"][-1]["spec"]["target"]["template"] = {
            "metadata": {"labels": {"argocd.argoproj.io/secret-type": "repository"}},
        }
        self.assertEqual(self.errors(), [
            "platform: ExternalSecret/preview-github-token may not label its Secret with argocd.argoproj.io/secret-type",
        ])

    def test_missing_render_for_restricted_project_is_rejected(self) -> None:
        del self.renders["prod.yaml"]
        self.assertIn(f"missing render: umc-product-server: {self.output / 'prod.yaml'}", self.errors())

    def test_main_fails_on_missing_render(self) -> None:
        del self.renders["prod.yaml"]
        self.errors()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(self.output), 1)


# Argo CD v3.5 IsGroupKindNamePermitted와 같은 판정인지 확인한다.
class ProjectSemanticsTest(unittest.TestCase):
    def test_absent_namespace_whitelist_permits_everything(self) -> None:
        self.assertTrue(Project("open", (), (), None, ()).permits(("", "Secret"), "x"))

    def test_empty_namespace_whitelist_denies_everything(self) -> None:
        self.assertFalse(Project("closed", (), (), (), ()).permits(("", "Service"), "x"))

    def test_namespace_blacklist_overrides_whitelist(self) -> None:
        project = Project("black", (), (), None, (("", "Secret"),))
        self.assertFalse(project.permits(("", "Secret"), "x"))
        self.assertTrue(project.permits(("", "Service"), "x"))

    def test_wildcard_entry_matches(self) -> None:
        self.assertTrue(Project("wild", (), (), (("traefik.io", "*"),), ()).permits(("traefik.io", "Middleware"), "x"))

    def test_empty_cluster_whitelist_rejects_cluster_kind(self) -> None:
        self.assertFalse(Project("closed", (), (), (("", "Service"),), ()).permits(("", "Namespace"), "x"))

    def test_cluster_whitelist_name_restricts(self) -> None:
        project = Project("named", (("", "Namespace", "app-*"),), (), None, ())
        self.assertTrue(project.permits(("", "Namespace"), "app-dev"))
        self.assertFalse(project.permits(("", "Namespace"), "kube-system"))


# Argo CD directory source처럼 yaml·yml·json을 읽고 exclude·include·skip marker를 따른다.
class DirectorySourceTest(unittest.TestCase):
    def test_reads_argo_manifest_suffixes_and_respects_filters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "nested").mkdir()
            for name in ("a.yaml", "b.yml", "c.json", "nested/d.yaml", "nested/skip.yaml", "excluded.json", "notes.txt"):
                (root / name).write_text("{}", encoding="utf-8")
            (root / "nested/skip.yaml").write_text("# +argocd:skip-file-rendering\n", encoding="utf-8")
            source = {"path": directory, "directory": {"recurse": True, "exclude": "{excluded.json,other/*}"}}
            self.assertEqual([path.relative_to(root).as_posix() for path in directory_renders(source)],
                             ["a.yaml", "b.yml", "c.json", "nested/d.yaml"])
            only_json = {"path": directory, "directory": {"include": "*.json"}}
            self.assertEqual([path.name for path in directory_renders(only_json)], ["c.json", "excluded.json"])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Tests for repository-wide contract validation helpers."""

from __future__ import annotations

import copy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from validate_contracts import (
    MAIL_CAPTURE_JAVA_TOOL_OPTIONS,
    git_visible_files,
    preview_cert_manager_resources,
    validate_preview_mail_capture_tls,
    validate_repository_identity,
)


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


RESTRICTED = {
    "allowPrivilegeEscalation": False,
    "readOnlyRootFilesystem": True,
    "capabilities": {"drop": ["ALL"]},
}
APP_IMAGE = "ghcr.io/umc-product/umc-product-server@sha256:" + "a" * 64


def preview_deployment() -> list[dict]:
    return [{
        "kind": "Deployment",
        "spec": {"template": {"spec": {
            "initContainers": [{
                "name": "mail-capture-truststore",
                "image": APP_IMAGE,
                "securityContext": copy.deepcopy(RESTRICTED),
                "volumeMounts": [
                    {"name": "mail-capture-truststore", "mountPath": "/truststore"},
                    {"name": "mail-capture-cert", "mountPath": "/mail-capture-cert"},
                ],
            }],
            "containers": [
                {
                    "name": "umc-product-server",
                    "image": APP_IMAGE,
                    "env": [{"name": "JAVA_TOOL_OPTIONS", "value": MAIL_CAPTURE_JAVA_TOOL_OPTIONS}],
                    "envFrom": [{"secretRef": {"name": "app-email"}}],
                    "volumeMounts": [{"name": "mail-capture-truststore", "mountPath": "/truststore"}],
                },
                {
                    "name": "mailpit",
                    "env": [{"name": "SMTP_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "app-email", "key": "SMTP_PASSWORD"}}}],
                    "volumeMounts": [{"name": "mailpit-tls", "mountPath": "/tls"}],
                },
            ],
            "volumes": [
                {"name": "mailpit-tls", "secret": {"secretName": "preview-mail-capture-loopback-tls"}},
                {"name": "mail-capture-cert", "secret": {
                    "secretName": "preview-mail-capture-loopback-tls",
                    "items": [{"key": "tls.crt", "path": "tls.crt"}],
                }},
                {"name": "mail-capture-truststore", "emptyDir": {}},
            ],
        }}},
    }]


# Preview PR Pod는 운영 host까지 덮는 공개 wildcard 키를 어떤 경로로도 참조하지 않아야 한다.
class PreviewMailCaptureTlsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.resources = preview_deployment()
        self.pod = self.resources[0]["spec"]["template"]["spec"]
        self.cert_manager = preview_cert_manager_resources()

    def validate(self) -> None:
        validate_preview_mail_capture_tls(self.resources, self.cert_manager)

    def test_accepts_loopback_certificate_mounted_only_by_mailpit(self) -> None:
        self.validate()

    def test_rejects_wildcard_secret_volume(self) -> None:
        self.pod["volumes"][0]["secret"]["secretName"] = "preview-wildcard-tls"
        with self.assertRaisesRegex(SystemExit, "wildcard TLS Secret"):
            self.validate()

    def test_rejects_wildcard_secret_in_projected_volume_or_env(self) -> None:
        self.pod["volumes"].append({"name": "x", "projected": {"sources": [
            {"secret": {"name": "preview-wildcard-tls"}},
        ]}})
        with self.assertRaisesRegex(SystemExit, "wildcard TLS Secret"):
            self.validate()
        self.pod["volumes"].pop()
        self.pod["containers"][0]["envFrom"].append({"secretRef": {"name": "preview-wildcard-tls"}})
        with self.assertRaisesRegex(SystemExit, "wildcard TLS Secret"):
            self.validate()

    def test_rejects_app_container_reading_loopback_private_key(self) -> None:
        self.pod["containers"][0]["volumeMounts"].append({"name": "mailpit-tls", "mountPath": "/tls"})
        with self.assertRaisesRegex(SystemExit, "only Mailpit may mount"):
            self.validate()

    def test_rejects_truststore_builder_receiving_private_key(self) -> None:
        self.pod["volumes"][1]["secret"]["items"].append({"key": "tls.key", "path": "tls.key"})
        with self.assertRaisesRegex(SystemExit, "only the public certificate"):
            self.validate()

    def test_rejects_missing_truststore_option(self) -> None:
        self.pod["containers"][0]["env"] = []
        with self.assertRaisesRegex(SystemExit, "built truststore"):
            self.validate()

    def test_rejects_public_issuer_for_loopback_certificate(self) -> None:
        loopback = next(
            item for item in self.cert_manager
            if item["kind"] == "Certificate"
            and item["metadata"]["name"] == "preview-mail-capture-loopback"
        )
        loopback["spec"]["issuerRef"] = {
            "group": "cert-manager.io", "kind": "ClusterIssuer", "name": "letsencrypt-production",
        }
        with self.assertRaisesRegex(SystemExit, "self-signed Issuer"):
            self.validate()


if __name__ == "__main__":
    unittest.main()

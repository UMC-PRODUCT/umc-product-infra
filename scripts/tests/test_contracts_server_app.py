#!/usr/bin/env python3
"""Tests for umc-product-server Preview render contracts."""

from __future__ import annotations

import copy
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from contracts.server_app import (
    MAIL_CAPTURE_JAVA_TOOL_OPTIONS,
    preview_cert_manager_resources,
    validate_preview_dropdb_job,
    validate_preview_mail_capture_tls,
)

REPO_ROOT = SCRIPTS_DIR.parent
CHART = REPO_ROOT / "charts" / "umc-product-server"


# PR 종료 hook은 이름 검사 뒤 공용 DB 연결을 기다린 다음에만 DROP을 보내고, 그 대기가 deadline 안에 들어가야 한다.
@unittest.skipUnless(shutil.which("helm") and shutil.which("bash"), "helm or bash not installed")
class PreviewDropdbJobTest(unittest.TestCase):
    TARGET = "umc_product_pr42"
    DROP = 'DROP DATABASE IF EXISTS :"db" WITH (FORCE);'

    def render(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                "helm", "template", "umc-product-preview-42", str(CHART),
                "--namespace", "preview",
                "-f", str(CHART / "values-preview.yaml"),
                "--set", "deployment.enabled=true",
                "--set-string", "image.tag=0123456789ab",
                "--set-string", "image.digest=sha256:" + "a" * 64,
                "--set-string", "fullnameOverride=umc-product-server-pr-42",
                "--set-string", "ingress.host=api-pr-42.university.neordinary.com",
                "--set-string", "ingress.tls[0].hosts[0]=api-pr-42.university.neordinary.com",
                "--set-string", "createDatabase.name=" + self.TARGET,
                "--set-string", "env.DATABASE_URL=jdbc:postgresql://postgres-preview.preview.svc.cluster.local:5432/" + self.TARGET,
                "--set-string", "env.SPRING_APPLICATION_NAME=preview-umc-product-pr42",
                "--set-string", "env.APP_ENVIRONMENT=preview-pr-42",
                "--set-string", "env.DEMODAY_QR_BASE_URL=https://university.neordinary.com",
                "--set-string", "env.SSO_ISSUER=https://api-pr-42.university.neordinary.com",
                "--set-string", "env.CERTIFICATE_VERIFICATION_URL_TEMPLATE=https://api-pr-42.university.neordinary.com/api/v1/certificates/verify/{serialNumber}",
                "--set-string", "mailCapture.uiHost=mail-pr-42.university.neordinary.com",
                "--show-only", "templates/dropdb-job.yaml",
                *extra,
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def setUp(self) -> None:
        result = self.render()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.job = yaml.safe_load(result.stdout)
        self.container = self.job["spec"]["template"]["spec"]["containers"][0]

    def run_hook(self, *, failures: int | None, seconds: int = 0, target: str | None = None) -> tuple[subprocess.CompletedProcess, list[str]]:
        # 가짜 pg_isready는 failures번 거부한 뒤 연결을 받는다(None이면 계속 거부).
        # bash는 환경의 SECONDS를 시작값으로 쓰므로 seconds로 경과 시간을 정한다.
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        calls = root / "calls"
        fakes = {
            "pg_isready": f"""echo pg_isready >> "{calls}"
n=$(grep -c '^pg_isready$' "{calls}")
[ "$READY_AFTER" != never ] && [ "$n" -gt "$READY_AFTER" ]
""",
            "psql": f"""printf 'psql %s :: %s\\n' "$*" "$(cat)" >> "{calls}"
""",
            "sleep": "exit 0\n",
        }
        (root / "bin").mkdir()
        for name, body in fakes.items():
            fake = root / "bin" / name
            fake.write_text("#!/bin/sh\n" + body)
            fake.chmod(0o755)
        env = {item["name"]: item["value"] for item in self.container["env"] if "value" in item}
        if target is not None:
            env["TARGET_DB"] = target
        env.update(
            PATH=f"{root / 'bin'}:/usr/bin:/bin",
            SECONDS=str(seconds),
            READY_AFTER="never" if failures is None else str(failures),
        )
        result = subprocess.run(
            [*self.container["command"], self.container["args"][0]],
            env=env, capture_output=True, text=True, timeout=10, check=False,
        )
        return result, calls.read_text().splitlines() if calls.exists() else []

    def test_accepts_rendered_job(self) -> None:
        validate_preview_dropdb_job(self.job)

    def test_drops_target_once_after_connection_refused(self) -> None:
        result, calls = self.run_hook(failures=2, seconds=590)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            calls,
            ["pg_isready"] * 3 + [f"psql -v ON_ERROR_STOP=1 --set=db={self.TARGET} :: {self.DROP}"],
        )

    def test_logs_wait_start_before_connecting(self) -> None:
        result, _ = self.run_hook(failures=0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("PostgreSQL 연결 대기: postgres-preview."), result.stdout)

    def test_fails_without_drop_after_600_seconds(self) -> None:
        result, calls = self.run_hook(failures=None, seconds=600)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("PostgreSQL 연결 대기 실패", result.stderr)
        self.assertEqual(calls, ["pg_isready"])

    def test_rejects_disallowed_name_without_connecting(self) -> None:
        for target in ("postgres", "umc_product_dev", "42", "umc_product_pr", "umc_product_pr42x"):
            with self.subTest(target=target):
                result, calls = self.run_hook(failures=0, target=target)
                self.assertEqual(result.returncode, 64, result.stderr)
                self.assertEqual(calls, [])

    def test_rejects_non_post_delete_hook(self) -> None:
        self.job["metadata"]["annotations"]["argocd.argoproj.io/hook"] = "PostSync"
        with self.assertRaisesRegex(SystemExit, "after workload deletion"):
            validate_preview_dropdb_job(self.job)

    def test_rejects_unquoted_drop(self) -> None:
        self.container["args"][0] = self.container["args"][0].replace(self.DROP, "DROP DATABASE umc_product_pr42;")
        with self.assertRaisesRegex(SystemExit, "quoted target database"):
            validate_preview_dropdb_job(self.job)

    def test_rejects_sh_without_seconds(self) -> None:
        self.container["command"] = ["sh", "-c"]
        with self.assertRaisesRegex(SystemExit, "run under bash"):
            validate_preview_dropdb_job(self.job)

    def test_rejects_deadline_shorter_than_wait(self) -> None:
        self.job["spec"]["activeDeadlineSeconds"] = 899
        with self.assertRaisesRegex(SystemExit, "deadline must cover"):
            validate_preview_dropdb_job(self.job)

    def test_chart_refuses_deadline_shorter_than_wait(self) -> None:
        result = self.render("--set", "deleteDatabase.activeDeadlineSeconds=899")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("deleteDatabase.activeDeadlineSeconds", result.stderr)


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
        with self.assertRaisesRegex(SystemExit, "only Mailpit may read"):
            self.validate()

    def test_rejects_app_container_reading_loopback_key_outside_secret_volumes(self) -> None:
        secret = "preview-mail-capture-loopback-tls"
        bypasses = {
            "envFrom": lambda pod, app: app["envFrom"].append({"secretRef": {"name": secret}}),
            "projected": lambda pod, app: (
                pod["volumes"].append({"name": "key", "projected": {"sources": [
                    {"secret": {"name": secret}},
                ]}}),
                app["volumeMounts"].append({"name": "key", "mountPath": "/key"}),
            ),
            "secretKeyRef": lambda pod, app: app["env"].append({"name": "KEY", "valueFrom": {
                "secretKeyRef": {"name": secret, "key": "tls.key"},
            }}),
        }
        for name, add_reference in bypasses.items():
            with self.subTest(name):
                self.setUp()
                add_reference(self.pod, self.pod["containers"][0])
                with self.assertRaisesRegex(SystemExit, "only Mailpit may read"):
                    self.validate()

    def test_accepts_app_container_reading_only_public_certificate(self) -> None:
        self.pod["containers"][0]["env"].append({"name": "CERT", "valueFrom": {
            "secretKeyRef": {"name": "preview-mail-capture-loopback-tls", "key": "tls.crt"},
        }})
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

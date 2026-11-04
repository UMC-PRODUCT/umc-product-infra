#!/usr/bin/env python3
"""Tests for repository-wide contract validation helpers."""

from __future__ import annotations

import copy
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
    MAIL_CAPTURE_JAVA_TOOL_OPTIONS,
    external_dns_hosts,
    git_visible_files,
    pod_resource_totals,
    preview_cert_manager_resources,
    validate_app_storage_contract,
    validate_cloudformation_tags,
    validate_edge_protection,
    validate_external_dns_record_names,
    validate_preview_budget_arithmetic,
    validate_preview_mail_capture_tls,
    validate_repository_identity,
    validate_route53_contract,
    validate_ses_contract,
)

REPO_ROOT = SCRIPTS_DIR.parent
CHART = REPO_ROOT / "charts" / "umc-product-server"


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
            shutil.copy(REPO_ROOT / relative, self.root / relative)

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
        with patch("validate_contracts.ROOT", self.root):
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


ZONE = "university.neordinary.com"
ALLOWED_PATTERNS = [
    f"api.{ZONE}",
    f"api-pr-*.{ZONE}",
    f"mail-pr-*.{ZONE}",
    f"_external-dns.a-api.{ZONE}",
    f"_external-dns.a-api-pr-*.{ZONE}",
    f"_external-dns.a-mail-pr-*.{ZONE}",
]
RENDERED_HOSTS = {f"api.{ZONE}", f"api-pr-42.{ZONE}", f"mail-pr-42.{ZONE}"}
OPT_IN_FILTER = "external-dns.kubernetes.io/managed-by=umc-infra"
OPT_IN_ANNOTATIONS = {"external-dns.kubernetes.io/managed-by": "umc-infra"}


# ExternalDNS가 만들 A·소유권 TXT 이름은 모두 IAM이 허용하고, IAM 패턴은 렌더된 이름보다 넓지 않아야 한다.
class ExternalDnsRecordNameTest(unittest.TestCase):
    def validate(self, patterns: list[str], hosts: set[str]) -> None:
        validate_external_dns_record_names(
            patterns, hosts, txt_prefix="_external-dns.", record_types=["A", "TXT"]
        )

    def test_accepts_rendered_hosts_with_ownership_txt(self) -> None:
        self.validate(ALLOWED_PATTERNS, RENDERED_HOSTS)

    def test_rejects_new_host_without_iam_permission(self) -> None:
        with self.assertRaisesRegex(SystemExit, "does not allow record name: .*docs-pr-42"):
            self.validate(ALLOWED_PATTERNS, RENDERED_HOSTS | {f"docs-pr-42.{ZONE}"})

    def test_rejects_host_without_ownership_txt_permission(self) -> None:
        patterns = [p for p in ALLOWED_PATTERNS if p != f"_external-dns.a-mail-pr-*.{ZONE}"]
        with self.assertRaisesRegex(
            SystemExit, f"record name: _external-dns.a-mail-pr-42.{ZONE}"
        ):
            self.validate(patterns, RENDERED_HOSTS)

    def test_rejects_zone_wide_wildcard(self) -> None:
        for broad in (f"*.{ZONE}", f"_external-dns.a-*.{ZONE}", f"mail-*.{ZONE}"):
            with self.subTest(broad=broad), self.assertRaisesRegex(
                SystemExit, "broader than a rendered record"
            ):
                self.validate([*ALLOWED_PATTERNS, broad], RENDERED_HOSTS)

    def test_rejects_pattern_without_rendered_host(self) -> None:
        with self.assertRaisesRegex(SystemExit, f"no longer used: grafana.{ZONE}"):
            self.validate([*ALLOWED_PATTERNS, f"grafana.{ZONE}"], RENDERED_HOSTS)

    def test_collects_only_opted_in_ingress_hosts(self) -> None:
        documents = [
            {
                "kind": "Ingress",
                "metadata": {
                    "annotations": {
                        **OPT_IN_ANNOTATIONS,
                        "external-dns.kubernetes.io/hostname": f"extra.{ZONE}, alias.{ZONE}",
                    }
                },
                "spec": {
                    "rules": [{"host": f"api.{ZONE}"}],
                    "tls": [{"hosts": [f"tls-only.{ZONE}"]}],
                },
            },
            {
                "kind": "Ingress",
                "metadata": {"annotations": {}},
                "spec": {"rules": [{"host": f"unmanaged.{ZONE}"}]},
            },
            {"kind": "Service", "metadata": {"annotations": OPT_IN_ANNOTATIONS}},
        ]

        self.assertEqual(
            external_dns_hosts(documents, OPT_IN_FILTER),
            {f"api.{ZONE}", f"tls-only.{ZONE}", f"extra.{ZONE}", f"alias.{ZONE}"},
        )

    def test_rejects_annotation_filter_it_cannot_evaluate(self) -> None:
        for annotation_filter in (
            "external-dns.kubernetes.io/managed-by in (umc-infra)",
            "external-dns.kubernetes.io/managed-by!=other",
            f"{OPT_IN_FILTER},team=infra",
        ):
            with self.subTest(annotation_filter=annotation_filter), self.assertRaisesRegex(
                SystemExit, "annotationFilter must be a single key=value"
            ):
                external_dns_hosts([], annotation_filter)

    # ApplicationSet 렌더의 Preview host가 바뀌면 커밋된 Route 53 IAM 허용 목록과 대조해 실패해야 한다.
    def test_rejects_applicationset_host_missing_from_committed_iam(self) -> None:
        ingress = {
            "kind": "Ingress",
            "metadata": {"annotations": OPT_IN_ANNOTATIONS},
            "spec": {"rules": [{"host": f"inbox-pr-42.{ZONE}"}]},
        }
        with tempfile.TemporaryDirectory() as directory:
            render = Path(directory, "preview-pr-42-desired-state.yaml")
            render.write_text(yaml.safe_dump(ingress), encoding="utf-8")
            with self.assertRaisesRegex(
                SystemExit, "does not allow record name: .*inbox-pr-42"
            ):
                validate_route53_contract([render])


if __name__ == "__main__":
    unittest.main()

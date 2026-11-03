#!/usr/bin/env python3
"""Regression checks for the public IDC target and matching DNS filter."""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import validate_edge_platform
from validate_edge_platform import validate_preview_wildcard_certificate, validate_target_gate


# 단일 공인 IDC 주소만 DNS 필터와 Grafana에 함께 허용하고 넓은 CIDR·사설·테스트 주소는 거부한다.
class TargetGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.base = {
            "externalDNS": {"target": "1.255.226.166"},
            "certificate": {"issuerRef": {"name": "letsencrypt-production"}},
        }
        self.grafana_values = {
            "ingress": {
                "enabled": False,
                "annotations": {"external-dns.kubernetes.io/target": "1.255.226.166"},
            },
            "extraObjects": [{
                "kind": "Certificate",
                "spec": {"issuerRef": {"name": "letsencrypt-production"}},
            }],
        }

    def validate(self, target_filter: str = "1.255.226.166/32") -> None:
        disabled = {"ingress": {"enabled": False}, "deployment": {"enabled": False}}
        grafana = {"spec": {"source": {"helm": {"valuesObject": self.grafana_values}}}}
        with (
            patch("validate_edge_platform.documents", return_value=[grafana]),
            patch("validate_edge_platform.Path.read_text", return_value=""),
            patch("validate_edge_platform.yaml.safe_load", side_effect=[self.base] + [disabled] * 3),
        ):
            validate_target_gate(target_filter, "ZEXAMPLE", "letsencrypt-production")

    def test_cafe24_target_accepts_matching_filter_and_grafana(self) -> None:
        self.validate()

    def test_mismatched_or_broad_filter_is_rejected(self) -> None:
        for target_filter in ("1.255.226.165/32", "1.255.226.0/24"):
            with self.subTest(target_filter=target_filter), self.assertRaisesRegex(
                SystemExit, "ExternalDNS filter must match IDC IPv4"
            ):
                self.validate(target_filter)

    def test_mismatched_grafana_target_is_rejected(self) -> None:
        self.grafana_values["ingress"]["annotations"][
            "external-dns.kubernetes.io/target"
        ] = "1.255.226.165"
        with self.assertRaisesRegex(SystemExit, "Grafana target must match IDC IPv4"):
            self.validate()

    def test_private_and_test_net_targets_are_rejected(self) -> None:
        for target in ("10.0.0.1", "203.0.113.10"):
            self.base["externalDNS"]["target"] = target
            with self.subTest(target=target), self.assertRaisesRegex(
                SystemExit, "IDC target must be a public IPv4"
            ):
                self.validate(f"{target}/32")


# 공개 CA Certificate는 중앙 wildcard 하나뿐이다. namespaced ACME Issuer로 두 번째 공개 인증서를 만들 수 없어야 한다.
class PreviewCertificateSetTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        shutil.copytree(
            validate_edge_platform.ROOT / "manifests" / "cert-manager",
            self.root / "manifests" / "cert-manager",
        )

    def validate(self, extra: str = "") -> None:
        (self.root / "manifests" / "cert-manager" / "extra.yaml").write_text(extra)
        with patch("validate_edge_platform.ROOT", self.root):
            validate_preview_wildcard_certificate()

    def test_accepts_repository_certificates(self) -> None:
        self.validate()

    def test_rejects_namespaced_acme_issuer(self) -> None:
        with self.assertRaisesRegex(SystemExit, "namespaced Issuers must be selfSigned"):
            self.validate("""
apiVersion: cert-manager.io/v1
kind: Issuer
metadata: {name: acme, namespace: preview}
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    privateKeySecretRef: {name: acme-account}
    solvers: [{http01: {ingress: {}}}]
""")

    def test_rejects_extra_certificate_from_namespaced_issuer(self) -> None:
        with self.assertRaisesRegex(SystemExit, "only the wildcard and Mailpit loopback"):
            self.validate("""
apiVersion: cert-manager.io/v1
kind: Certificate
metadata: {name: preview-api, namespace: preview}
spec:
  secretName: preview-api-tls
  dnsNames: [api.university.neordinary.com]
  issuerRef: {group: cert-manager.io, kind: Issuer, name: preview-selfsigned}
""")


if __name__ == "__main__":
    unittest.main()

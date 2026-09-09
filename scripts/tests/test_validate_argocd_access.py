#!/usr/bin/env python3
"""Regression checks for the public HTTPS and isolated Argo CD backend."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from validate_argocd import ACCESS_DIR, PUBLIC_HOST, validate_access_manifests, validate_access_release


class AccessManifestsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.resources = [yaml.safe_load(path.read_text(encoding="utf-8"))
                          for path in sorted(ACCESS_DIR.glob("*.yaml"))]
        self.by_kind = {item["kind"]: item for item in self.resources}

    def test_reviewed_access_is_valid(self) -> None:
        validate_access_manifests(self.resources)

    def test_unreviewed_ingress_changes_are_rejected(self) -> None:
        annotations = self.by_kind["Ingress"]["metadata"]["annotations"]
        for key, value in (
            ("external-dns.kubernetes.io/target", "1.255.226.165"),
            ("traefik.ingress.kubernetes.io/router.entrypoints", "web"),
        ):
            with self.subTest(annotation=key):
                original = annotations[key]
                annotations[key] = value
                with self.assertRaisesRegex(SystemExit, "reviewed HTTPS"):
                    validate_access_manifests(self.resources)
                annotations[key] = original

    def test_staging_certificate_is_rejected(self) -> None:
        self.by_kind["Certificate"]["spec"]["issuerRef"]["name"] = "letsencrypt-staging"
        with self.assertRaisesRegex(SystemExit, "production Argo CD certificate"):
            validate_access_manifests(self.resources)

    def test_wrong_backend_port_is_rejected(self) -> None:
        self.by_kind["Ingress"]["spec"]["rules"][0]["http"]["paths"][0][
            "backend"]["service"]["port"] = {"name": "https"}
        with self.assertRaisesRegex(SystemExit, "HTTP Service backend"):
            validate_access_manifests(self.resources)

    def test_allow_all_policy_is_rejected(self) -> None:
        self.by_kind["NetworkPolicy"]["spec"]["ingress"] = [{}]
        with self.assertRaisesRegex(SystemExit, "only Traefik"):
            validate_access_manifests(self.resources)

    def test_namespace_and_pod_selectors_cannot_be_split(self) -> None:
        peers = self.by_kind["NetworkPolicy"]["spec"]["ingress"][0]["from"]
        peers.append({"podSelector": peers[0].pop("podSelector")})
        with self.assertRaisesRegex(SystemExit, "only Traefik"):
            validate_access_manifests(self.resources)

    def test_certificate_and_isolation_must_precede_ingress(self) -> None:
        for kind in ("Certificate", "NetworkPolicy"):
            with self.subTest(kind=kind):
                annotations = self.by_kind[kind]["metadata"]["annotations"]
                annotations["argocd.argoproj.io/sync-wave"] = "2"
                with self.assertRaisesRegex(SystemExit, "precede the public Ingress"):
                    validate_access_manifests(self.resources)
                annotations["argocd.argoproj.io/sync-wave"] = "-2"


class AccessReleaseTest(unittest.TestCase):
    def setUp(self) -> None:
        labels = {"app.kubernetes.io/name": "argocd-server", "app.kubernetes.io/instance": "argocd"}
        self.service = {"type": "ClusterIP", "selector": labels,
                        "ports": [{"name": "http", "port": 80, "targetPort": 8080}]}
        self.template = {"metadata": {"labels": labels}, "spec": {"containers": [{
            "ports": [{"name": "server", "containerPort": 8080}],
        }]}}
        self.documents = [
            {"kind": "ConfigMap", "metadata": {"name": "argocd-cm"},
             "data": {"url": f"https://{PUBLIC_HOST}"}},
            {"kind": "ConfigMap", "metadata": {"name": "argocd-cmd-params-cm"},
             "data": {"server.insecure": "true"}},
            {"kind": "Service", "metadata": {"name": "argocd-server"}, "spec": self.service},
            {"kind": "Deployment", "metadata": {"name": "argocd-server"},
             "spec": {"template": self.template}},
        ] + [self.policy(name) for name in (
            "argocd-application-controller", "argocd-dex-server", "argocd-redis", "argocd-repo-server",
        )]

    @staticmethod
    def policy(name: str) -> dict:
        return {"kind": "NetworkPolicy", "metadata": {"name": name}, "spec": {
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": name}},
        }}

    def test_reviewed_release_is_valid(self) -> None:
        validate_access_release(self.documents)

    def test_broad_chart_server_policy_is_rejected(self) -> None:
        self.documents.append(self.policy("argocd-server"))
        with self.assertRaisesRegex(SystemExit, "without a broad server policy"):
            validate_access_release(self.documents)

    def test_removing_other_component_policy_is_rejected(self) -> None:
        self.documents.pop()
        with self.assertRaisesRegex(SystemExit, "preserve other component policies"):
            validate_access_release(self.documents)

    def test_external_service_is_rejected(self) -> None:
        self.service["type"] = "LoadBalancer"
        with self.assertRaisesRegex(SystemExit, "private ClusterIP"):
            validate_access_release(self.documents)

    def test_service_cannot_bypass_isolated_port(self) -> None:
        self.service["ports"][0]["targetPort"] = 8083
        with self.assertRaisesRegex(SystemExit, "server Pod port 8080"):
            validate_access_release(self.documents)

    def test_host_network_is_rejected(self) -> None:
        self.template["spec"]["hostNetwork"] = True
        with self.assertRaisesRegex(SystemExit, "without host networking"):
            validate_access_release(self.documents)


if __name__ == "__main__":
    unittest.main()

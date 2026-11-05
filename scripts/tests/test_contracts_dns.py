#!/usr/bin/env python3
"""Tests for Route 53 and ExternalDNS record name contracts."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import yaml

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from contracts.dns import (
    PREVIEW_HOST_MIGRATION_PATTERNS,
    external_dns_hosts,
    validate_external_dns_record_names,
    validate_route53_contract,
)


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

    # Preview host 이전 기간에는 렌더되지 않은 이전 대상 패턴만 IAM에 남길 수 있다.
    def test_accepts_preview_migration_patterns_without_rendered_host(self) -> None:
        self.validate([*ALLOWED_PATTERNS, *sorted(PREVIEW_HOST_MIGRATION_PATTERNS)], RENDERED_HOSTS)

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

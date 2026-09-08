#!/usr/bin/env python3
"""Regression checks for the public IDC target and matching DNS filter."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from validate_edge_platform import validate_target_gate


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


if __name__ == "__main__":
    unittest.main()

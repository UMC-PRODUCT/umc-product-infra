#!/usr/bin/env python3
"""Tests for app Ingress edge protection contracts."""

from __future__ import annotations

import shutil
import subprocess
import sys
import unittest
from pathlib import Path

import yaml

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from contracts.edge import validate_edge_protection

REPO_ROOT = SCRIPTS_DIR.parent
CHART = REPO_ROOT / "charts" / "umc-product-server"


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

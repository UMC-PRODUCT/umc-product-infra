#!/usr/bin/env python3
"""Regression checks for Operator discovery and authorization boundaries."""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from validate_observability import (
    PROMETHEUS_NAME,
    ManifestLoader,
    pod_spec,
    selector_matches,
    validate_cluster_resource,
    validate_monitor_selection,
    validate_monitoring_rbac,
    workload_labels,
)


class OperatorContractsTest(unittest.TestCase):
    def test_operator_crd_matcher_enum_keeps_literal_equals(self) -> None:
        self.assertEqual(yaml.load("enum: [=, '!=', '=~', '!~']", Loader=ManifestLoader),
                         {"enum": ["=", "!=", "=~", "!~"]})

    def setUp(self) -> None:
        self.prometheus = {
            "kind": "Prometheus", "metadata": {"name": PROMETHEUS_NAME},
            "spec": {
                "image": "quay.io/prometheus/prometheus@sha256:" + "a" * 64,
                "resources": {"requests": {"cpu": "100m", "memory": "768Mi"},
                              "limits": {"memory": "1Gi"}},
                "serviceMonitorSelector": {"matchLabels": {"release": "prometheus"}},
                "serviceMonitorNamespaceSelector": {
                    "matchLabels": {"kubernetes.io/metadata.name": "monitoring"}},
            },
        }
        self.monitor = {
            "kind": "ServiceMonitor",
            "metadata": {"name": "postgres", "namespace": "monitoring",
                         "labels": {"release": "prometheus"}},
            "spec": {"namespaceSelector": {"matchNames": ["db"]},
                     "selector": {"matchLabels": {"app": "postgres-exporter"}},
                     "endpoints": [{"port": "metrics"}]},
        }
        self.service = {
            "kind": "Service",
            "metadata": {"name": "postgres-exporter", "namespace": "db",
                         "labels": {"app": "postgres-exporter"}},
            "spec": {"ports": [{"name": "metrics", "port": 9187}]},
        }

    def test_monitor_selects_service_across_namespace(self) -> None:
        validate_monitor_selection(self.prometheus, [self.monitor], [self.service])

    def test_wrong_release_or_monitor_namespace_is_rejected(self) -> None:
        for field, value in (("namespace", "db"), ("labels", {"release": "other"})):
            monitor = copy.deepcopy(self.monitor)
            monitor["metadata"][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(SystemExit, "not selected"):
                validate_monitor_selection(self.prometheus, [monitor], [self.service])

    def test_numeric_port_and_wrong_service_labels_are_rejected(self) -> None:
        self.monitor["spec"]["endpoints"][0]["port"] = 9187
        with self.assertRaisesRegex(SystemExit, "named Service port"):
            validate_monitor_selection(self.prometheus, [self.monitor], [self.service])
        self.monitor["spec"]["endpoints"][0]["port"] = "metrics"
        self.service["metadata"]["labels"] = {"app": "database"}
        with self.assertRaisesRegex(SystemExit, "matches no Service"):
            validate_monitor_selection(self.prometheus, [self.monitor], [self.service])

    def test_missing_named_port_is_rejected(self) -> None:
        self.service["spec"]["ports"][0]["name"] = "http"
        with self.assertRaisesRegex(SystemExit, "missing from selected Service"):
            validate_monitor_selection(self.prometheus, [self.monitor], [self.service])

    def test_operator_workload_remains_in_image_resource_and_network_checks(self) -> None:
        workload = pod_spec(self.prometheus)
        self.assertEqual(workload["containers"][0]["image"], self.prometheus["spec"]["image"])
        self.assertEqual(workload["containers"][0]["resources"], self.prometheus["spec"]["resources"])
        self.assertTrue(selector_matches({"matchLabels": {
            "app.kubernetes.io/name": "prometheus", "operator.prometheus.io/name": PROMETHEUS_NAME,
        }}, workload_labels(self.prometheus)))
        self.assertFalse(selector_matches({"matchLabels": {
            "app.kubernetes.io/instance": "prometheus", "app.kubernetes.io/component": "server",
        }}, workload_labels(self.prometheus)))

    def test_prometheus_cluster_exception_does_not_open_other_apps(self) -> None:
        role = {"kind": "ClusterRole", "metadata": {"name": "metrics"}, "rules": []}
        validate_cluster_resource("prometheus", role)
        with self.assertRaisesRegex(SystemExit, "forbidden cluster resource"):
            validate_cluster_resource("grafana", role)
        with self.assertRaisesRegex(SystemExit, "forbidden cluster resource"):
            validate_cluster_resource("prometheus", {
                "kind": "ValidatingWebhookConfiguration", "metadata": {"name": "unexpected"}})

    def test_cluster_write_secret_and_wildcard_permissions_are_rejected(self) -> None:
        for resources, verbs in ((["pods"], ["*"]), (["secrets"], ["get"]), (["pods"], ["patch"])):
            role = {"kind": "ClusterRole", "metadata": {"name": "metrics"},
                    "rules": [{"apiGroups": [""], "resources": resources, "verbs": verbs}]}
            with self.subTest(resources=resources, verbs=verbs), self.assertRaises(SystemExit):
                validate_monitoring_rbac([role])


if __name__ == "__main__":
    unittest.main()

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
    ALERTMANAGER_NAME,
    ROOT,
    ManifestLoader,
    pod_spec,
    selector_matches,
    validate_alertmanager_config,
    validate_grafana_dashboard_startup,
    validate_loki_query_budget,
    validate_cluster_resource,
    validate_monitor_selection,
    validate_monitoring_rbac,
    validate_preview_application_name,
    workload_labels,
)


# 수집 selector/포트가 끊기거나 Operator의 예외 권한이 다른 관측 앱으로 퍼지면 실패해야 한다.
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


# 전역 알림 설정은 Secret 참조만 사용하고 기존 base Secret·평문 webhook과 혼용하지 않는다.
class AlertmanagerNativeConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.resources = [
            {"kind": "Alertmanager", "metadata": {"name": ALERTMANAGER_NAME},
             "spec": {"alertmanagerConfiguration": {"name": "prometheus-discord"}}},
            {"apiVersion": "monitoring.coreos.com/v1alpha1", "kind": "AlertmanagerConfig",
             "metadata": {"name": "prometheus-discord", "namespace": "monitoring"},
             "spec": {"route": {"receiver": "discord", "routes": [{"receiver": "null", "matchers": [
                 {"name": "application", "matchType": "=~", "value": "preview-umc-product-pr[0-9]+"}]}]},
                 "receivers": [
                 {"name": "discord", "discordConfigs": [{"apiURL": {
                     "name": "alertmanager-discord", "key": "discord-webhook"}}]}],
                 "inhibitRules": [{
                     "sourceMatch": [{"name": "severity", "matchType": "=", "value": "critical"}],
                     "targetMatch": [{"name": "severity", "matchType": "=", "value": "warning"}],
                     "equal": ["alertname", "application", "namespace", "job", "instance"]}, {
                     "sourceMatch": [{"name": "alertname", "matchType": "=", "value": "ApiHigh5xxErrorRatio"}],
                     "targetMatch": [{"name": "alertname", "matchType": "=~",
                                      "value": "ApiHigh5xxErrorCount|ApiRouteHigh5xxErrorCount"}],
                     "equal": ["application"]}]}},
        ]

    def test_global_config_uses_secret_reference_without_legacy_base_yaml(self) -> None:
        validate_alertmanager_config(self.resources)

    def test_namespace_selected_config_is_not_a_global_route(self) -> None:
        self.resources[0]["spec"] = {"alertmanagerConfigSelector": {"matchLabels": {"release": "prometheus"}}}
        with self.assertRaisesRegex(SystemExit, "must be the global configuration"):
            validate_alertmanager_config(self.resources)

    def test_wrong_secret_key_and_plaintext_webhook_are_rejected(self) -> None:
        discord = self.resources[1]["spec"]["receivers"][0]["discordConfigs"][0]
        for value in ({"name": "alertmanager-discord", "key": "wrong-key"},
                      "https://example.invalid/synthetic-webhook"):
            discord["apiURL"] = value
            with self.subTest(value=value), self.assertRaisesRegex(SystemExit, "SecretKeySelector"):
                validate_alertmanager_config(self.resources)

    def test_preview_app_alerts_cannot_reach_discord(self) -> None:
        routes = self.resources[1]["spec"]["route"]["routes"]
        for broken in ([], [{"receiver": "discord", "matchers": routes[0]["matchers"]}],
                       [{"receiver": "null", "matchers": [
                           {"name": "environment", "matchType": "=~", "value": "preview-pr-.*"}]}]):
            self.resources[1]["spec"]["route"]["routes"] = broken
            with self.subTest(routes=broken), self.assertRaisesRegex(SystemExit, "Preview PR app"):
                validate_alertmanager_config(self.resources)

    def test_preview_drop_route_tolerates_fields_that_keep_it_terminal(self) -> None:
        self.resources[1]["spec"]["route"]["routes"][0].update({"groupWait": "10s", "continue": False})
        validate_alertmanager_config(self.resources)
        for extra in ({"continue": True}, {"routes": [{"receiver": "discord"}]}):
            route = {**self.resources[1]["spec"]["route"]["routes"][0], **extra}
            self.resources[1]["spec"]["route"]["routes"] = [route]
            with self.subTest(extra=extra), self.assertRaisesRegex(SystemExit, "Preview PR app"):
                validate_alertmanager_config(self.resources)

    # KSM 알림은 job·instance가 같아서 namespace가 빠지면 app OOM critical이 preview OOM warning을 덮는다.
    def test_critical_cannot_inhibit_warnings_in_another_namespace(self) -> None:
        self.resources[1]["spec"]["inhibitRules"][0]["equal"] = [
            "alertname", "service_name", "application", "job", "instance"]
        with self.assertRaisesRegex(SystemExit, "same alertname and namespace"):
            validate_alertmanager_config(self.resources)

    def test_5xx_count_warnings_stay_grouped_under_ratio_critical(self) -> None:
        self.resources[1]["spec"]["inhibitRules"] = self.resources[1]["spec"]["inhibitRules"][:1]
        with self.assertRaisesRegex(SystemExit, "count-based 5xx"):
            validate_alertmanager_config(self.resources)

    def test_legacy_base_yaml_cannot_return_alongside_native_config(self) -> None:
        self.resources.append({"kind": "Secret", "metadata": {
            "name": f"alertmanager-{ALERTMANAGER_NAME}"}, "stringData": {
                "alertmanager.yaml": "receivers: [{name: discord, discord_configs: [{webhook_url_file: /old-path}]}]"}})
        with self.assertRaisesRegex(SystemExit, "replace the legacy base Secret"):
            validate_alertmanager_config(self.resources)


# Discord drop route의 이름 pattern은 ApplicationSet이 PR 앱에 붙이는 이름에서 나온다.
class PreviewApplicationNameTest(unittest.TestCase):
    def setUp(self) -> None:
        self.appset = yaml.safe_load(
            (ROOT / "argocd/applications/preview/applicationset.yaml").read_text(encoding="utf-8"))
        self.parameter = next(item for item in self.appset["spec"]["template"]["spec"]["source"]["helm"]["parameters"]
                              if item["name"] == "env.SPRING_APPLICATION_NAME")

    def test_applicationset_name_matches_preview_pattern(self) -> None:
        validate_preview_application_name(self.appset)

    def test_renamed_preview_app_is_rejected(self) -> None:
        for value in ("umc-product-preview-pr{{ .number }}", "preview-umc-product-{{ .number }}",
                      "preview-umc-product-pr42"):
            self.parameter["value"] = value
            with self.subTest(value=value), self.assertRaisesRegex(SystemExit, "PREVIEW_APPLICATION_PATTERN"):
                validate_preview_application_name(self.appset)


# 끝나지 않는 WATCH를 일반 initContainer에 두면 Grafana가 시작하지 못하는 회귀를 재현한다.
class GrafanaStartupTest(unittest.TestCase):
    def test_dashboard_watch_cannot_block_as_a_regular_init_container(self) -> None:
        resources = [{"kind": "Deployment", "metadata": {"name": "grafana"}, "spec": {
            "template": {"spec": {"initContainers": [{"name": "grafana-init-sc-dashboard",
                "env": [{"name": "METHOD", "value": "WATCH"}]}]}}}}]
        with self.assertRaisesRegex(SystemExit, "not a blocking init container"):
            validate_grafana_dashboard_startup(resources)


class LokiQueryBudgetTest(unittest.TestCase):
    def test_단일_바이너리의_쿼리_실행과_분할_병렬도를_제한한다(self) -> None:
        bounded = {"querier": {"max_concurrent": 2}, "limits_config": {
            "max_query_parallelism": 2, "tsdb_max_query_parallelism": 2}}
        validate_loki_query_budget(bounded)
        for section, key in (("querier", "max_concurrent"),
                             ("limits_config", "max_query_parallelism"),
                             ("limits_config", "tsdb_max_query_parallelism")):
            for value in (None, 0, 128):
                broken = copy.deepcopy(bounded)
                if value is None:
                    del broken[section][key]
                else:
                    broken[section][key] = value
                with self.subTest(key=key, value=value), self.assertRaisesRegex(SystemExit, "Loki"):
                    validate_loki_query_budget(broken)


if __name__ == "__main__":
    unittest.main()

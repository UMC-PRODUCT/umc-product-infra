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
    PROMETHEUS_QUERY_PROXY_URL,
    ROOT,
    ManifestLoader,
    admits_prometheus_ingress,
    pod_spec,
    selector_matches,
    validate_alertmanager_config,
    validate_grafana_dashboard_startup,
    validate_grafana_datasources,
    validate_k3s_traefik_metrics,
    validate_traefik_pod_monitor_job,
    validate_preview_exporter_pod_monitor_job,
    validate_loki_ingestion_limits,
    validate_loki_memory,
    validate_loki_query_budget,
    validate_loki_retention,
    validate_cluster_resource,
    validate_monitor_selection,
    validate_monitoring_rbac,
    validate_preview_application_name,
    validate_prometheus_ingress_sources,
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

    def test_pod_monitor_needs_a_selected_pod_with_the_named_container_port(self) -> None:
        self.prometheus["spec"].update(
            podMonitorSelector={"matchLabels": {"release": "prometheus"}},
            podMonitorNamespaceSelector={"matchLabels": {"kubernetes.io/metadata.name": "monitoring"}})
        monitor = {
            "kind": "PodMonitor",
            "metadata": {"name": "traefik", "namespace": "monitoring", "labels": {"release": "prometheus"}},
            "spec": {"namespaceSelector": {"matchNames": ["kube-system"]},
                     "selector": {"matchLabels": {"app.kubernetes.io/name": "traefik"}},
                     "podMetricsEndpoints": [{"port": "metrics"}]},
        }
        pod = {"kind": "Pod", "metadata": {"name": "traefik", "namespace": "kube-system",
                                            "labels": {"app.kubernetes.io/name": "traefik"}},
               "spec": {"containers": [{"name": "traefik", "ports": [{"name": "metrics", "containerPort": 9100}]}]}}
        validate_monitor_selection(self.prometheus, [monitor], [], [pod])
        mutations = {
            "matches no Pod": lambda m, p: p["metadata"].update(namespace="monitoring"),
            "missing from selected Pod": lambda m, p: p["spec"]["containers"][0]["ports"][0].update(name="web"),
            "named container port": lambda m, p: m["spec"]["podMetricsEndpoints"][0].update(port=9100),
        }
        for message, mutate in mutations.items():
            broken_monitor, broken_pod = copy.deepcopy(monitor), copy.deepcopy(pod)
            mutate(broken_monitor, broken_pod)
            with self.subTest(message), self.assertRaisesRegex(SystemExit, message):
                validate_monitor_selection(self.prometheus, [broken_monitor], [], [broken_pod])

    def test_traefik_pod_model_must_be_rechecked_when_k3s_or_its_metrics_values_change(self) -> None:
        k3s_version = yaml.safe_load((ROOT / "ansible/roles/k3s/defaults/main.yml").read_text())["k3s_version"]
        config = yaml.safe_load((ROOT / "ansible/roles/k3s/templates/traefik-config.yaml.j2").read_text())
        validate_k3s_traefik_metrics(k3s_version, config)
        values = yaml.safe_load(config["spec"]["valuesContent"])
        overrides = {
            "metrics": {**values, "metrics": {"prometheus": None}},
            "ports.metrics": {**values, "ports": {**values["ports"], "metrics": {"port": 9200}}},
        }
        for name, broken in overrides.items():
            broken_config = copy.deepcopy(config)
            broken_config["spec"]["valuesContent"] = yaml.safe_dump(broken)
            with self.subTest(name), self.assertRaisesRegex(SystemExit, "HelmChartConfig"):
                validate_k3s_traefik_metrics(k3s_version, broken_config)
        with self.assertRaisesRegex(SystemExit, "K3S_TRAEFIK_VERIFIED_VERSION"):
            validate_k3s_traefik_metrics("v0.0.0+k3s1", config)

    def test_traefik_pod_monitor_job_label_matches_the_missing_metrics_alert(self) -> None:
        monitor = next(resource for resource in yaml.safe_load_all(
            (ROOT / "manifests/observability-integrations/pod-monitors.yaml").read_text())
            if resource and resource["metadata"]["name"] == "traefik")
        validate_traefik_pod_monitor_job(monitor)
        dropped = copy.deepcopy(monitor)
        del dropped["spec"]["jobLabel"]
        instance = copy.deepcopy(monitor)
        instance["spec"]["jobLabel"] = "app.kubernetes.io/instance"
        for name, broken in {"dropped": dropped, "instance label": instance}.items():
            with self.subTest(name), self.assertRaisesRegex(SystemExit, "jobLabel"):
                validate_traefik_pod_monitor_job(broken)

    def test_preview_exporter_pod_monitor_job_stays_apart_from_prod_alerts(self) -> None:
        monitor = next(resource for resource in yaml.safe_load_all(
            (ROOT / "manifests/observability-integrations/pod-monitors.yaml").read_text())
            if resource and resource["metadata"]["name"] == "postgres-preview-exporter")
        deployment = yaml.safe_load(
            (ROOT / "manifests/postgres/preview/postgres-exporter.yaml").read_text())
        validate_preview_exporter_pod_monitor_job(monitor, deployment)
        dropped = copy.deepcopy(monitor)
        del dropped["spec"]["jobLabel"]
        relabeled = copy.deepcopy(monitor)
        relabeled["spec"]["podMetricsEndpoints"][0]["relabelings"] = [
            {"targetLabel": "job", "replacement": "postgres-exporter"}]
        for name, broken in {"dropped": dropped, "relabeled to prod job": relabeled}.items():
            with self.subTest(name), self.assertRaisesRegex(SystemExit, "postgres-preview-exporter PodMonitor"):
                validate_preview_exporter_pod_monitor_job(broken, deployment)

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
                 {"name": "application", "matchType": "=~", "value": "preview-umc-product-pr[0-9]+"}]},
                 {"receiver": "null", "matchers": [
                     {"name": "job", "matchType": "=", "value": "argocd-application-controller-metrics"},
                     {"name": "name", "matchType": "=~", "value": "umc-product-preview-[0-9]+"}]}]},
                 "receivers": [
                 {"name": "discord", "discordConfigs": [{"apiURL": {
                     "name": "alertmanager-discord", "key": "discord-webhook"}}]}],
                 "inhibitRules": [{
                     "sourceMatch": [{"name": "severity", "matchType": "=", "value": "critical"}],
                     "targetMatch": [{"name": "severity", "matchType": "=", "value": "warning"}],
                     "equal": ["alertname", "application", "namespace", "job", "instance", "name"]}, {
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

    # Preview PR Application의 Argo CD 알림만 버리고 ArgoCDMetricsMissing처럼 name이 없는 알림은 남긴다.
    def test_preview_argocd_application_alerts_cannot_reach_discord(self) -> None:
        routes = self.resources[1]["spec"]["route"]["routes"]
        argocd_route = routes[1]
        for broken in (routes[:1], [routes[0], {**argocd_route, "receiver": "discord"}],
                       [routes[0], {**argocd_route, "matchers": argocd_route["matchers"][1:]}],
                       [routes[0], routes[0], argocd_route]):
            self.resources[1]["spec"]["route"]["routes"] = broken
            with self.subTest(routes=broken), self.assertRaisesRegex(SystemExit, "Preview PR Argo CD"):
                validate_alertmanager_config(self.resources)

    # KSM 알림은 job·instance가 같아서 namespace가 빠지면 app OOM critical이 preview OOM warning을 덮는다.
    # Argo CD 앱 알림은 namespace·job이 모두 같아 name이 빠지면 운영 앱 critical이 다른 앱 warning을 덮는다.
    def test_critical_cannot_inhibit_warnings_of_another_target(self) -> None:
        for missing in ("namespace", "name"):
            self.resources[1]["spec"]["inhibitRules"][0]["equal"] = [
                label for label in ["alertname", "service_name", "application", "namespace", "job", "instance", "name"]
                if label != missing]
            with self.subTest(missing=missing), self.assertRaisesRegex(
                    SystemExit, "same alertname, namespace and name"):
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
            with self.subTest(value=value), self.assertRaisesRegex(SystemExit, "PREVIEW_SPRING_APP_PATTERN"):
                validate_preview_application_name(self.appset)

    def test_renamed_preview_argocd_application_is_rejected(self) -> None:
        for value in ("umc-product-server-preview-{{ .number }}", "umc-product-preview-42"):
            self.appset["spec"]["template"]["metadata"]["name"] = value
            with self.subTest(value=value), self.assertRaisesRegex(
                    SystemExit, "PREVIEW_ARGOCD_APP_PATTERN"):
                validate_preview_application_name(self.appset)


# Prometheus가 exporter에 닿으려면 target namespace의 policy가 monitoring namespace의 Prometheus Pod를
# metrics port로 들여야 한다.
class PrometheusIngressPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.prometheus_labels = {"app.kubernetes.io/name": "prometheus",
                                  "operator.prometheus.io/name": PROMETHEUS_NAME}
        self.target = {"kind": "Deployment", "metadata": {"name": "exporter", "namespace": "db"},
                       "spec": {"template": {"metadata": {"labels": {"app.kubernetes.io/name": "exporter"}}}}}
        self.policy = {
            "kind": "NetworkPolicy", "metadata": {"name": "exporter-allow-prometheus", "namespace": "db"},
            "spec": {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "exporter"}},
                     "ingress": [{"from": [{
                         "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "monitoring"}},
                         "podSelector": {"matchLabels": {"app.kubernetes.io/name": "prometheus"}}}],
                         "ports": [{"protocol": "TCP", "port": 9187}]}]},
        }

    def admits(self) -> bool:
        return admits_prometheus_ingress([self.policy], self.target, self.prometheus_labels, {"metrics", 9187})

    def test_policy_admitting_monitoring_prometheus_on_metrics_port_passes(self) -> None:
        self.assertTrue(self.admits())
        del self.policy["spec"]["ingress"][0]["ports"]
        self.assertTrue(self.admits())

    def test_policy_that_does_not_reach_prometheus_is_rejected(self) -> None:
        def peer(policy: dict) -> dict:
            return policy["spec"]["ingress"][0]["from"][0]
        mutations = {
            "same-namespace peer": lambda policy: peer(policy).pop("namespaceSelector"),
            "other namespace": lambda policy: peer(policy).update(
                namespaceSelector={"matchLabels": {"kubernetes.io/metadata.name": "app"}}),
            "other Pods": lambda policy: peer(policy).update(
                podSelector={"matchLabels": {"app.kubernetes.io/name": "grafana"}}),
            "other port": lambda policy: policy["spec"]["ingress"][0].update(ports=[{"port": 5432}]),
            "other target": lambda policy: policy["spec"].update(
                podSelector={"matchLabels": {"app.kubernetes.io/name": "db"}}),
            "policy in another namespace": lambda policy: policy["metadata"].update(namespace="monitoring"),
        }
        for case, mutate in mutations.items():
            self.setUp()
            mutate(self.policy)
            with self.subTest(case):
                self.assertFalse(self.admits())


# Grafana나 namespace 전체가 Prometheus Pod에 직접 닿으면 조회 proxy의 쓰기·관리 API 차단을 우회한다.
class PrometheusIngressSourcesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.prometheus_labels = workload_labels({"kind": "Prometheus", "metadata": {"name": PROMETHEUS_NAME},
                                                  "spec": {}})
        self.workloads = {
            f"Prometheus/{PROMETHEUS_NAME}": self.prometheus_labels,
            "Deployment/otel-collector": {"app.kubernetes.io/name": "opentelemetry-collector",
                                          "app.kubernetes.io/instance": "otel-collector",
                                          "component": "standalone-collector"},
            "Deployment/prometheus-query-proxy": {"app.kubernetes.io/name": "prometheus-query-proxy"},
            "Deployment/grafana": {"app.kubernetes.io/name": "grafana", "app.kubernetes.io/instance": "grafana"},
            "StatefulSet/loki": {"app.kubernetes.io/name": "loki"},
        }
        path = ROOT / "manifests/observability/networkpolicy.yaml"
        self.policies = [policy for policy in yaml.safe_load_all(path.read_text()) if policy]

    def policy(self, name: str) -> dict:
        return next(policy for policy in self.policies if policy["metadata"]["name"] == name)

    def validate(self) -> None:
        validate_prometheus_ingress_sources(self.policies, self.workloads, self.prometheus_labels)

    def test_committed_policies_admit_only_collector_proxy_and_prometheus(self) -> None:
        self.validate()

    def prometheus_rule(self) -> dict:
        return self.policy("prometheus-allow-required-ingress")["spec"]["ingress"][0]

    def test_other_sources_are_rejected(self) -> None:
        mutations = {
            "Grafana peer": lambda: self.prometheus_rule()["from"].append(
                {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "grafana"}}}),
            "every monitoring Pod": lambda: self.prometheus_rule()["from"].append({"podSelector": {}}),
            "rule without from": lambda: self.prometheus_rule().pop("from"),
            "namespace peer": lambda: self.prometheus_rule()["from"][0].update(namespaceSelector={}),
            "ipBlock peer": lambda: self.prometheus_rule()["from"].append({"ipBlock": {"cidr": "10.42.0.0/16"}}),
            "selector matching no workload": lambda: self.prometheus_rule()["from"].append(
                {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "future"}}}),
            "Prometheus back in same-namespace policy": lambda: self.policy("monitoring-allow-same-namespace")[
                "spec"]["podSelector"]["matchExpressions"][0]["values"].remove("prometheus"),
        }
        for case, mutate in mutations.items():
            self.setUp()
            mutate()
            with self.subTest(case), self.assertRaises(SystemExit):
                self.validate()


# Prometheus datasource가 조회 proxy를 건너뛰거나 쓰기 대상으로 열리면 실패해야 한다.
class GrafanaDatasourcesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.datasources = [
            {"uid": "prometheus", "type": "prometheus", "url": PROMETHEUS_QUERY_PROXY_URL,
             "jsonData": {"timeInterval": "30s"}},
            {"uid": "alertmanager", "type": "alertmanager",
             "url": f"http://{ALERTMANAGER_NAME}.monitoring.svc.cluster.local:9093"},
        ]

    def test_prometheus_through_query_proxy_passes(self) -> None:
        validate_grafana_datasources(self.datasources)

    def test_direct_prometheus_url_is_rejected(self) -> None:
        self.datasources[0]["url"] = f"http://{PROMETHEUS_NAME}.monitoring.svc.cluster.local:9090"
        with self.assertRaisesRegex(SystemExit, "read-only query proxy"):
            validate_grafana_datasources(self.datasources)

    def test_prometheus_type_is_rejected(self) -> None:
        self.datasources[0]["jsonData"]["prometheusType"] = "Prometheus"
        with self.assertRaisesRegex(SystemExit, "prometheusType"):
            validate_grafana_datasources(self.datasources)


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


# 9월 OOM 경로를 다시 여는 변경(상한 삭제·0·상향, GOMEMLIMIT 재과소·과대, WAL 재생 상한 누락)을 막는다.
class LokiIngestionLimitsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = {
            "limits_config": {
                "ingestion_rate_mb": 4, "ingestion_burst_size_mb": 16,
                "per_stream_rate_limit": "2MB", "per_stream_rate_limit_burst": "6MB",
                "max_line_size": "64KB", "max_line_size_truncate": True,
                "max_structured_metadata_size": "64KB", "max_global_streams_per_user": 2000,
            },
            "ingester": {"wal": {"replay_memory_ceiling": "768MB"}},
        }
        self.resources = [{"kind": "StatefulSet", "metadata": {"name": "loki"}, "spec": {"template": {"spec": {
            "containers": [{"name": "loki", "env": [{"name": "GOMEMLIMIT", "value": "1200MiB"}],
                            "resources": {"limits": {"memory": "1.5Gi"}}}]}}}}]

    def test_명시한_상한과_메모리_설정을_통과시킨다(self) -> None:
        validate_loki_ingestion_limits(self.config)
        validate_loki_memory(self.resources, self.config)

    def test_상한을_지우거나_0이나_더_큰_값으로_바꾸면_실패한다(self) -> None:
        for key, raised in (("ingestion_rate_mb", 8), ("ingestion_burst_size_mb", 32),
                            ("per_stream_rate_limit", "3MB"), ("per_stream_rate_limit_burst", "15MB"),
                            ("max_line_size", "256KB"), ("max_structured_metadata_size", "128KB"),
                            ("max_global_streams_per_user", 5000)):
            for value in (None, 0, raised):
                broken = copy.deepcopy(self.config)
                if value is None:
                    del broken["limits_config"][key]
                else:
                    broken["limits_config"][key] = value
                with self.subTest(key=key, value=value), self.assertRaisesRegex(SystemExit, key):
                    validate_loki_ingestion_limits(broken)

    def test_더_낮춘_상한은_허용한다(self) -> None:
        self.config["limits_config"].update(ingestion_rate_mb=2, per_stream_rate_limit="512KB")
        validate_loki_ingestion_limits(self.config)

    def test_긴_줄을_버리도록_바꾸면_실패한다(self) -> None:
        self.config["limits_config"]["max_line_size_truncate"] = False
        with self.assertRaisesRegex(SystemExit, "max_line_size_truncate"):
            validate_loki_ingestion_limits(self.config)

    def test_GOMEMLIMIT가_limit에서_벗어나거나_WAL_재생_상한이_없으면_실패한다(self) -> None:
        env = self.resources[0]["spec"]["template"]["spec"]["containers"][0]["env"][0]
        for value in ("512MiB", "1536MiB", "2GiB", "1200M"):
            env["value"] = value
            with self.subTest(value=value), self.assertRaisesRegex(SystemExit, "GOMEMLIMIT"):
                validate_loki_memory(self.resources, self.config)
        env["value"] = "1200MiB"
        for ceiling in (None, "4GB"):
            config = copy.deepcopy(self.config)
            if ceiling is None:
                del config["ingester"]["wal"]["replay_memory_ceiling"]
            else:
                config["ingester"]["wal"]["replay_memory_ceiling"] = ceiling
            with self.subTest(ceiling=ceiling), self.assertRaisesRegex(SystemExit, "replay_memory_ceiling"):
                validate_loki_memory(self.resources, config)

    def test_Pod_template이_없으면_검증_실패로_멈춘다(self) -> None:
        del self.resources[0]["spec"]["template"]["spec"]
        with self.assertRaisesRegex(SystemExit, "Loki: StatefulSet must have a Pod template"):
            validate_loki_memory(self.resources, self.config)


# compactor 삭제는 되돌릴 수 없으므로 보존기간이 늘거나 줄어드는 어떤 변경도 막는다.
class LokiRetentionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.limits = {
            "retention_period": "2160h",
            "retention_stream": [
                {"selector": '{service_name=~"preview-umc-product-pr[0-9]+"}', "priority": 1, "period": "168h"},
                {"selector": '{service_name="dev-umc-product"}', "priority": 1, "period": "720h"},
            ],
        }

    def assert_rejected(self, pattern: str) -> None:
        with self.assertRaisesRegex(SystemExit, pattern):
            validate_loki_retention({"limits_config": self.limits})

    def test_운영_90일과_환경별_보존기간을_통과시킨다(self) -> None:
        validate_loki_retention({"limits_config": self.limits})

    def test_운영_보존기간을_바꾸면_실패한다(self) -> None:
        self.limits["retention_period"] = "720h"
        self.assert_rejected("90 days")

    def test_같은_selector를_더_높은_priority로_추가하면_위치와_상관없이_실패한다(self) -> None:
        for index in (0, 2):
            with self.subTest(index=index):
                self.setUp()
                self.limits["retention_stream"].insert(
                    index, {"selector": '{service_name="dev-umc-product"}', "priority": 2, "period": "9999h"})
                self.assert_rejected("retention_stream")

    def test_priority나_period를_바꾸거나_규칙을_빼면_실패한다(self) -> None:
        for key, value in (("priority", 0), ("period", "336h")):
            with self.subTest(key=key):
                self.setUp()
                self.limits["retention_stream"][0][key] = value
                self.assert_rejected("retention_stream")
        self.setUp()
        self.limits["retention_stream"].pop(0)
        self.assert_rejected("retention_stream")

    def test_null이면_TypeError가_아니라_검증_실패로_끝난다(self) -> None:
        self.limits["retention_stream"] = None
        self.assert_rejected("retention_stream")
        self.limits["retention_stream"] = [None]
        self.assert_rejected("retention_stream")
        with self.assertRaisesRegex(SystemExit, "90 days"):
            validate_loki_retention({"limits_config": None})


if __name__ == "__main__":
    unittest.main()

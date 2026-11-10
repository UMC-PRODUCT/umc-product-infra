#!/usr/bin/env python3
"""Render pinned observability charts and validate their shared runtime contract."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, TypeVar

import yaml

from chart_downloads import template_chart


ROOT = Path(__file__).resolve().parent.parent
T = TypeVar("T")
APPLICATION_DIR = ROOT / "argocd" / "applications" / "platform" / "observability"
EXPECTED_APPLICATIONS = {"grafana", "loki", "otel-collector", "prometheus", "tempo"}
PROMETHEUS_NAME = "prometheus-kube-prometheus-prometheus"
ALERTMANAGER_NAME = "prometheus-kube-prometheus-alertmanager"
# Preview는 dev profile로 떠서 environment label로는 dev와 구분되지 않는다.
# Spring 앱 지표의 application label과 Argo CD Application 이름은 서로 다른 pattern이다.
PREVIEW_SPRING_APP_PATTERN = "preview-umc-product-pr[0-9]+"
PREVIEW_ARGOCD_APP_PATTERN = "umc-product-preview-[0-9]+"
# ServiceMonitor에 jobLabel이 없어 job label은 metrics Service 이름이다.
ARGOCD_METRICS_JOB = "argocd-application-controller-metrics"
# K3s가 설치하는 Traefik Pod의 label. Grafana Ingress policy와 Traefik PodMonitor가 같은 Pod를 가리켜야 한다.
K3S_TRAEFIK_POD_LABELS = {"app.kubernetes.io/name": "traefik", "app.kubernetes.io/instance": "traefik-kube-system"}
# main()의 Traefik Pod 모델(metrics port 이름·label)을 내장 chart에서 확인한 K3s 버전. K3s를 올리면 다시 확인한다.
K3S_TRAEFIK_VERIFIED_VERSION = "v1.36.3+k3s1"
MONITORING_CLUSTER_KINDS = {
    "ClusterRole", "ClusterRoleBinding", "CustomResourceDefinition"
}
CLUSTER_SCOPED_KINDS = {
    "APIService",
    "ClusterRole",
    "ClusterRoleBinding",
    "CustomResourceDefinition",
    "MutatingWebhookConfiguration",
    "Namespace",
    "PersistentVolume",
    "PodSecurityPolicy",
    "PriorityClass",
    "StorageClass",
    "ValidatingWebhookConfiguration",
}
IMAGE_DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
# Loki(datasize)·Kubernetes·Go GOMEMLIMIT 크기 표기. 이 저장소에서 쓰는 단위는 모두 1024 배수다.
BYTE_UNITS = {"": 1, "B": 1, "KB": 1 << 10, "MB": 1 << 20, "GB": 1 << 30,
              "Ki": 1 << 10, "Mi": 1 << 20, "Gi": 1 << 30,
              "KiB": 1 << 10, "MiB": 1 << 20, "GiB": 1 << 30}
# 9월 Loki OOM 재발 방지 상한. 값을 낮추는 것은 허용하고, 빠지거나 0(무제한)이거나 커지면 실패한다.
LOKI_INGESTION_CEILINGS = {
    "ingestion_rate_mb": 4,
    "ingestion_burst_size_mb": 16,
    "per_stream_rate_limit": "2MB",
    "per_stream_rate_limit_burst": "6MB",
    "max_line_size": "64KB",
    "max_structured_metadata_size": "64KB",
    "max_global_streams_per_user": 2000,
}
# Loki는 겹치는 selector 중 priority가 높은 규칙을 쓰므로 항목 추가만으로도 보존기간이 바뀐다. 목록 전체를 고정한다.
LOKI_STREAM_RETENTION = [
    ('{service_name=~"preview-umc-product-pr[0-9]+"}', 1, "168h"),
    ('{service_name="dev-umc-product"}', 1, "720h"),
]


class ManifestLoader(yaml.SafeLoader):
    """Accept the literal '=' enum in Operator CRDs (YAML 1.1 value tag)."""


def construct_value_tag(loader: ManifestLoader, node: yaml.ScalarNode) -> str:
    return loader.construct_scalar(node)


ManifestLoader.add_constructor("tag:yaml.org,2002:value", construct_value_tag)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"observability validation failed: {message}")


def present(value: T | None, message: str) -> T:
    """require(value is not None)처럼 실패하고, 통과한 값은 None이 아닌 타입으로 돌려준다."""
    require(value is not None, message)
    assert value is not None
    return value


def pod_spec(resource: dict) -> dict | None:
    kind = resource.get("kind")
    spec = resource.get("spec", {})
    if kind in {"DaemonSet", "Deployment", "Job", "StatefulSet"}:
        return spec.get("template", {}).get("spec")
    if kind == "CronJob":
        return (
            spec.get("jobTemplate", {})
            .get("spec", {})
            .get("template", {})
            .get("spec")
        )
    if kind == "Pod":
        return spec
    if kind in {"Prometheus", "Alertmanager"}:
        # The operator creates the StatefulSet later; validate its desired main
        # container and every explicitly configured extra container now.
        return {
            "containers": [{"name": kind.lower(), "image": spec.get("image"),
                            "resources": spec.get("resources", {})}]
            + spec.get("containers", []),
            "initContainers": spec.get("initContainers", []),
        }
    return None


def workload_labels(resource: dict) -> dict[str, str]:
    kind = resource.get("kind")
    if kind in {"Prometheus", "Alertmanager"}:
        name = resource["metadata"]["name"]
        # Reserved labels documented by the Prometheus Operator API.
        return {
            **resource["spec"].get("podMetadata", {}).get("labels", {}),
            "app.kubernetes.io/name": kind.lower(),
            "app.kubernetes.io/instance": name,
            "app.kubernetes.io/managed-by": "prometheus-operator",
            **({"operator.prometheus.io/name": name} if kind == "Prometheus" else {}),
            kind.lower(): name,
        }
    if kind == "Pod":
        return resource.get("metadata", {}).get("labels", {})
    spec = resource.get("spec", {})
    if kind == "CronJob":
        spec = spec.get("jobTemplate", {}).get("spec", {})
    return spec.get("template", {}).get("metadata", {}).get("labels", {})


def selector_matches(selector: dict, labels: dict[str, str]) -> bool:
    for key, value in selector.get("matchLabels", {}).items():
        if labels.get(key) != value:
            return False
    for expression in selector.get("matchExpressions", []):
        key = expression["key"]
        operator = expression["operator"]
        values = expression.get("values", [])
        require(operator in {"In", "NotIn", "Exists", "DoesNotExist"},
                f"unsupported selector operator: {operator}")
        if operator == "In" and labels.get(key) not in values:
            return False
        if operator == "NotIn" and key in labels and labels[key] in values:
            return False
        if operator == "Exists" and key not in labels:
            return False
        if operator == "DoesNotExist" and key in labels:
            return False
    return True


def configmap_value(resources: list[dict], name: str, key: str) -> str:
    matches = [
        resource
        for resource in resources
        if resource.get("kind") == "ConfigMap"
        and resource.get("metadata", {}).get("name") == name
    ]
    require(len(matches) == 1, f"ConfigMap/{name}: expected one resource")
    require(key in matches[0].get("data", {}), f"ConfigMap/{name}: missing {key}")
    return matches[0]["data"][key]


def named_resource(resources: list[dict], kind: str, name: str) -> dict:
    matches = [resource for resource in resources if resource.get("kind") == kind
               and resource.get("metadata", {}).get("name") == name]
    require(len(matches) == 1, f"{kind}/{name}: expected one resource")
    return matches[0]


def validate_cluster_resource(application: str, resource: dict) -> None:
    kind = resource["kind"]
    if kind not in CLUSTER_SCOPED_KINDS:
        return
    require(application == "prometheus" and kind in MONITORING_CLUSTER_KINDS,
            f"{application}: forbidden cluster resource {kind}/{resource['metadata']['name']}")
    if kind == "CustomResourceDefinition":
        require(resource["spec"].get("group") == "monitoring.coreos.com",
                "prometheus: only Prometheus Operator CRDs are allowed")
    if kind == "ClusterRoleBinding":
        require(resource.get("roleRef", {}).get("name") != "cluster-admin",
                "prometheus: cluster-admin binding is forbidden")
        require(all(subject.get("kind") == "ServiceAccount"
                    and subject.get("namespace") == "monitoring"
                    and subject.get("name") != "default"
                    for subject in resource.get("subjects", [])),
                "prometheus: cluster RBAC subjects must be dedicated monitoring accounts")


# 모니터링의 조회 권한이 cluster 쓰기·Secret 조회·와일드카드 권한으로 넓어지는 것을 막는다.
def validate_monitoring_rbac(resources: list[dict]) -> None:
    for resource in resources:
        if resource.get("kind") not in {"Role", "ClusterRole"}:
            continue
        for rule in resource.get("rules", []):
            require(all("*" not in rule.get(field, [])
                        for field in ("apiGroups", "resources", "verbs", "nonResourceURLs")),
                    f"{resource['kind']}/{resource['metadata']['name']}: wildcard RBAC is forbidden")
            if resource["kind"] == "ClusterRole":
                require(set(rule.get("verbs", [])) <= {"get", "list", "watch"},
                        "prometheus: cluster-wide writes are forbidden")
                require("secrets" not in rule.get("resources", []),
                        "prometheus: cluster-wide Secret access is forbidden")
    for resource in resources:
        if resource.get("kind") not in {"RoleBinding", "ClusterRoleBinding"}:
            continue
        role_ref = resource["roleRef"]
        named_resource(resources, role_ref["kind"], role_ref["name"])
        for subject in resource.get("subjects", []):
            require(subject.get("kind") == "ServiceAccount"
                    and subject.get("namespace") == "monitoring"
                    and subject.get("name") != "default",
                    "prometheus: RBAC bindings must use dedicated monitoring accounts")
            named_resource(resources, "ServiceAccount", subject["name"])


def operator_arguments(resources: list[dict]) -> dict[str, str]:
    deployment = named_resource(resources, "Deployment", "prometheus-kube-prometheus-operator")
    containers = deployment["spec"]["template"]["spec"]["containers"]
    operator = next(container for container in containers if container["name"] == "kube-prometheus-stack")
    return dict(argument.removeprefix("--").split("=", 1)
                for argument in operator.get("args", []) if "=" in argument)


# 실제 고정 chart의 CRD에서 스키마를 추출해 사용자 리소스 검증이 조용히 생략되지 않게 한다.
def write_monitoring_schemas(resources: list[dict], output_dir: Path) -> None:
    """Use the pinned chart's CRDs for kubeconform instead of a moving registry."""
    def strict_schema(value: Any) -> Any:
        if isinstance(value, list):
            return [strict_schema(item) for item in value]
        if not isinstance(value, dict):
            return value
        value = {key: strict_schema(item) for key, item in value.items()}
        if value.get("format") == "int-or-string":
            value.pop("format")
            value.pop("type", None)
            value["anyOf"] = [{"type": "integer"}, {"type": "string"}]
        if "properties" in value and not value.get("x-kubernetes-preserve-unknown-fields"):
            value.setdefault("additionalProperties", False)
        return value

    schema_dir = output_dir / "monitoring-schemas"
    schema_dir.mkdir(exist_ok=True)
    for resource in resources:
        if resource.get("kind") != "CustomResourceDefinition":
            continue
        for version in resource["spec"]["versions"]:
            if not version.get("served"):
                continue
            schema = strict_schema(version["schema"]["openAPIV3Schema"])
            filename = f"{resource['spec']['names']['kind']}_{version['name']}.json".lower()
            (schema_dir / filename).write_text(json.dumps(schema), encoding="utf-8")


def render_application(path: Path, output_dir: Path) -> tuple[str, list[dict]]:
    application = yaml.safe_load(path.read_text(encoding="utf-8"))
    require(application.get("kind") == "Application", f"{path}: expected Application")
    name = application["metadata"]["name"]
    spec = application["spec"]
    source = spec["source"]

    require(spec["project"] == "monitoring", f"{name}: AppProject")
    require(
        spec["destination"]["namespace"] == "monitoring",
        f"{name}: destination namespace",
    )
    require(source.get("targetRevision"), f"{name}: chart version must be pinned")
    require(source.get("repoURL", "").startswith("https://"), f"{name}: chart repository")

    command = [
        "helm",
        "template",
        name,
        source["chart"],
        "--repo",
        source["repoURL"],
        "--version",
        str(source["targetRevision"]),
        "--namespace",
        "monitoring",
        "--kube-version",
        "1.36.0",
        "--include-crds",
        "-f",
        "-",
    ]
    completed = template_chart(
        command,
        yaml.safe_dump(source.get("helm", {}).get("valuesObject", {})),
    )
    require(completed.returncode == 0, f"{name}: Helm render failed\n{completed.stderr}")

    output_path = output_dir / f"observability-{name}.yaml"
    output_path.write_text(completed.stdout, encoding="utf-8")
    resources = [
        document
        for document in yaml.load_all(completed.stdout, Loader=ManifestLoader)
        if isinstance(document, dict) and document.get("kind")
    ]
    require(bool(resources), f"{name}: chart rendered no resources")
    return name, resources


def validate_grafana_access_contract(application: dict) -> bool:
    values = application["spec"]["source"]["helm"]["valuesObject"]
    dashboards = values["sidecar"]["dashboards"]
    require(dashboards.get("initDashboards") is True,
            "grafana: dashboards must be provisioned before Grafana starts")
    ingress = values["ingress"]
    require(
        ingress.get("ingressClassName") == "traefik"
        and ingress.get("hosts") == ["grafana.university.neordinary.com"]
        and ingress.get("path") == "/"
        and ingress.get("pathType") == "Prefix",
        "grafana: canonical HTTPS Ingress contract",
    )
    require(
        ingress.get("annotations", {}).get(
            "traefik.ingress.kubernetes.io/router.entrypoints"
        )
        == "websecure"
        and ingress.get("tls")
        == [
            {
                "secretName": "grafana-university-neordinary-com-tls",
                "hosts": ["grafana.university.neordinary.com"],
            }
        ],
        "grafana: websecure entrypoint and TLS Secret",
    )

    config = values["grafana.ini"]
    require(
        config.get("dashboards")
        == {"default_home_dashboard_path": "/tmp/dashboards/UMC Product/system-overview.json"},
        "grafana: System Overview must be the default home dashboard",
    )
    require(
        config.get("server")
        == {
            "protocol": "http",
            "domain": "grafana.university.neordinary.com",
            "root_url": "https://grafana.university.neordinary.com",
            "enforce_domain": True,
        },
        "grafana: canonical public URL behind Traefik",
    )
    require(
        config.get("auth") == {"disable_login_form": False}
        and config.get("auth.basic")
        == {"enabled": True, "password_policy": True}
        and config.get("auth.anonymous") == {"enabled": False},
        "grafana: local login must be enabled and anonymous access disabled",
    )
    require(
        config.get("users")
        == {"allow_sign_up": False, "auto_assign_org_role": "Viewer"},
        "grafana: self-sign-up disabled and team accounts default to Viewer",
    )
    security = config.get("security", {})
    require(
        security.get("disable_brute_force_login_protection") is False
        and security.get("brute_force_login_protection_max_attempts") == 5
        and security.get("cookie_secure") is True
        and security.get("cookie_samesite") == "strict"
        and security.get("strict_transport_security") is True,
        "grafana: public login security settings",
    )
    return ingress.get("enabled") is True


def validate_loki_query_budget(config: dict) -> None:
    # 기본값으로 돌아가면 작은 단일 바이너리에서 장기간 대시보드 조회가 OOM을 유발할 수 있다.
    for section, key in (("querier", "max_concurrent"),
                         ("limits_config", "max_query_parallelism"),
                         ("limits_config", "tsdb_max_query_parallelism")):
        value = config.get(section, {}).get(key)
        require(isinstance(value, int) and 1 <= value <= 2,
                f"Loki: {section}.{key} must be explicitly bounded to 1..2 for 1.5Gi")


def byte_size(value: object) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    match = present(re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([A-Za-z]*)", str(value)),
                    f"unsupported byte size: {value!r}")
    require(match.group(2) in BYTE_UNITS, f"unsupported byte size: {value!r}")
    return float(match.group(1)) * BYTE_UNITS[match.group(2)]


def validate_loki_ingestion_limits(config: dict) -> None:
    limits = config.get("limits_config", {})
    for key, ceiling in LOKI_INGESTION_CEILINGS.items():
        value = limits.get(key)
        require(value is not None and not isinstance(value, bool)
                and 0 < byte_size(value) <= byte_size(ceiling),
                f"Loki: limits_config.{key} must be set, nonzero, and at most {ceiling}")
    require(limits.get("max_line_size_truncate") is True,
            "Loki: max_line_size_truncate must keep oversized lines instead of dropping them")


def validate_loki_retention(config: dict) -> None:
    limits = config.get("limits_config") or {}
    require(str(limits.get("retention_period")) == "2160h", "Loki: retention must remain 90 days")
    streams = present(limits.get("retention_stream"), "Loki: retention_stream must be a list of rules")
    require(isinstance(streams, list) and all(isinstance(item, dict) for item in streams),
            "Loki: retention_stream must be a list of rules")
    rules = [(item.get("selector"), item.get("priority"), item.get("period")) for item in streams]
    require(rules == LOKI_STREAM_RETENTION,
            "Loki: retention_stream must be exactly preview 7 days and dev 30 days")


# GOMEMLIMIT가 limit보다 너무 낮으면 GC가 과열되고, 너무 높으면 GC 전에 cgroup OOM kill이 난다.
def validate_loki_memory(resources: list[dict], config: dict) -> None:
    loki = present(pod_spec(named_resource(resources, "StatefulSet", "loki")),
                   "Loki: StatefulSet must have a Pod template")
    containers = [item for item in loki["containers"]
                  if item["name"] == "loki"]
    require(len(containers) == 1, "Loki: StatefulSet must run exactly one loki container")
    container = containers[0]
    limit = byte_size(container.get("resources", {}).get("limits", {}).get("memory", 0))
    environment = {item["name"]: item.get("value") for item in container.get("env", [])}
    require(re.fullmatch(r"[0-9]+(KiB|MiB|GiB)", str(environment.get("GOMEMLIMIT"))) is not None,
            "Loki: GOMEMLIMIT must be set in Go KiB/MiB/GiB units")
    soft_limit = byte_size(environment["GOMEMLIMIT"])
    require(limit > 0 and 0.6 * limit <= soft_limit <= 0.9 * limit,
            "Loki: GOMEMLIMIT must stay within 60..90% of the container memory limit")
    ceiling = config.get("ingester", {}).get("wal", {}).get("replay_memory_ceiling")
    require(ceiling is not None and 0 < byte_size(ceiling) < soft_limit,
            "Loki: WAL replay_memory_ceiling must stay below GOMEMLIMIT")


# 종료되지 않는 dashboard watcher를 일반 initContainer로 두어 Grafana 시작이 막히는 것을 방지한다.
def validate_grafana_dashboard_startup(resources: list[dict]) -> None:
    workload = present(pod_spec(named_resource(resources, "Deployment", "grafana")),
                       "grafana: Deployment must have a Pod template")
    initializers = [container for container in workload.get("initContainers", [])
                    if container.get("name") == "grafana-init-sc-dashboard"]
    require(len(initializers) == 1, "grafana: exactly one dashboard startup sidecar is required")
    sidecar = initializers[0]
    environment = {item["name"]: item.get("value") for item in sidecar.get("env", [])}
    require(sidecar.get("restartPolicy") == "Always" and environment.get("METHOD") == "WATCH",
            "grafana: dashboard WATCH must be a native sidecar, not a blocking init container")
    require(sidecar.get("startupProbe", {}).get("exec", {}).get("command") == [
                "python", "-c", 'import pathlib, sys; sys.exit(not pathlib.Path("/tmp/dashboards/UMC Product/system-overview.json").is_file())'],
            "grafana: startup must wait for the required System Overview dashboard file")
    grafana = next(container for container in workload["containers"] if container["name"] == "grafana")
    for container in (sidecar, grafana):
        require(any(mount.get("name") == "sc-dashboard-volume"
                    and mount.get("mountPath") == "/tmp/dashboards"
                    for mount in container.get("volumeMounts", [])),
                "grafana: startup sidecar and server must share the dashboard directory")
    require(not any(container.get("name") == "grafana-sc-dashboard"
                    for container in workload["containers"]),
            "grafana: only one dashboard watcher may run")


# 현재 generation의 reconcile과 가용성을 모두 확인해야 Argo CD가 정상으로 판단할 수 있다.
def validate_alertmanager_health_gate() -> None:
    values = yaml.safe_load((ROOT / "ansible/roles/argocd/files/argo-cd-values.yaml").read_text())
    config = {"data": values["configs"]["cm"]}
    health = config["data"].get("resource.customizations.health.monitoring.coreos.com_Alertmanager", "")
    generation_guard = health.find("condition.observedGeneration ~= obj.metadata.generation")
    healthy = health.find('hs.status = "Healthy"')
    require('ipairs({"Reconciled", "Available"})' in health
            and "condition.observedGeneration == nil" in health
            and generation_guard >= 0 and healthy > generation_guard
            and 'hs.status = "Degraded"' in health,
            "Alertmanager health must require current-generation Reconciled and Available conditions")
    require("resource.customizations.health.monitoring.coreos.com_Prometheus" not in config.get("data", {}),
            "Prometheus must retain Argo CD's built-in health check")


def validate_prometheus(resources: list[dict]) -> None:
    prometheus = named_resource(resources, "Prometheus", PROMETHEUS_NAME)
    spec = prometheus["spec"]
    require(spec.get("enableOTLPReceiver") is True,
            "prometheus: OTLP receiver must accept Collector metrics")
    require(spec.get("enableRemoteWriteReceiver") is True,
            "prometheus: remote write receiver must accept k6 results")
    require(spec.get("retention") == "14d", "prometheus: retention must remain 14 days")
    for selector in ("serviceMonitor", "podMonitor", "rule", "probe", "scrapeConfig"):
        require(spec.get(f"{selector}Selector") == {"matchLabels": {"release": "prometheus"}},
                f"prometheus: {selector} selector must use release=prometheus")
        require(spec.get(f"{selector}NamespaceSelector") == {
                    "matchLabels": {"kubernetes.io/metadata.name": "monitoring"}},
                f"prometheus: {selector} discovery must stay in monitoring")
    require(not spec.get("additionalScrapeConfigs"),
            "prometheus: scrape definitions must be owned by monitors")
    alertmanagers = spec.get("alerting", {}).get("alertmanagers", [])
    require(len(alertmanagers) == 1 and alertmanagers[0].get("name") == ALERTMANAGER_NAME
            and alertmanagers[0].get("namespace") == "monitoring"
            and alertmanagers[0].get("port") == "http-web",
            "prometheus: Alertmanager discovery must use the operator Service")
    for name, port in ((PROMETHEUS_NAME, 9090), (ALERTMANAGER_NAME, 9093)):
        service = named_resource(resources, "Service", name)
        require(service["spec"].get("type", "ClusterIP") == "ClusterIP"
                and any(p.get("name") == "http-web" and p.get("port") == port
                        for p in service["spec"].get("ports", [])),
                f"Service/{name}: internal named HTTP port")

    validate_alertmanager_config(resources)


# Discord drop route와 대시보드가 쓰는 Preview 이름 pattern이 실제 PR 앱 이름을 덮어야 한다.
def validate_preview_application_name(appset: dict) -> None:
    template = appset["spec"]["template"]
    parameters = template["spec"]["source"]["helm"]["parameters"]
    spring_name = next((item.get("value", "") for item in parameters
                        if item.get("name") == "env.SPRING_APPLICATION_NAME"), "")
    for field, value, constant, pattern in (
            ("SPRING_APPLICATION_NAME", spring_name, "PREVIEW_SPRING_APP_PATTERN", PREVIEW_SPRING_APP_PATTERN),
            ("Application name", template["metadata"].get("name", ""),
             "PREVIEW_ARGOCD_APP_PATTERN", PREVIEW_ARGOCD_APP_PATTERN)):
        require("{{ .number }}" in value and re.fullmatch(pattern, value.replace("{{ .number }}", "1494")) is not None,
                f"preview: ApplicationSet {field} must match {constant}")


def is_drop_route(route: dict, matchers: list[dict]) -> bool:
    return (route.get("receiver") == "null" and not route.get("continue") and not route.get("routes")
            and route.get("matchers") == matchers)


# target Pod를 고르는 NetworkPolicy가 monitoring namespace의 Prometheus Pod를 ports 중 하나로 들이는지 본다.
# namespaceSelector가 없는 peer는 policy와 같은 namespace의 Pod만 뜻하므로 인정하지 않는다.
def admits_prometheus_ingress(policies: list[dict], target: dict, prometheus_labels: dict[str, str],
                              ports: set[int | str]) -> bool:
    namespace = target["metadata"].get("namespace")
    target_labels = workload_labels(target)
    return any(
        "namespaceSelector" in peer
        and selector_matches(peer["namespaceSelector"], {"kubernetes.io/metadata.name": "monitoring"})
        and selector_matches(peer.get("podSelector", {}), prometheus_labels)
        and (not rule.get("ports") or any(port.get("port") in ports for port in rule["ports"]))
        for policy in policies if policy.get("kind") == "NetworkPolicy"
        and policy["metadata"].get("namespace") == namespace
        and selector_matches(policy["spec"].get("podSelector", {}), target_labels)
        for rule in policy["spec"].get("ingress", []) for peer in rule.get("from", []))


# 전역 알림 경로와 Secret 참조를 검사하고 기존 평문 기반 설정이 함께 되살아나는 것을 막는다.
def validate_alertmanager_config(resources: list[dict]) -> None:
    alertmanager = named_resource(resources, "Alertmanager", ALERTMANAGER_NAME)
    config_name = alertmanager["spec"].get("alertmanagerConfiguration", {}).get("name")
    require(config_name,
            "alertmanager: native Discord configuration must be the global configuration")
    native = named_resource(resources, "AlertmanagerConfig", config_name)
    require(native["metadata"].get("namespace") == "monitoring",
            "alertmanager: global configuration and Discord Secret must share monitoring namespace")
    config = native["spec"]
    require(config.get("route", {}).get("receiver") == "discord"
            and not config["route"].get("matchers"),
            "alertmanager: global route must deliver all namespaces to Discord")
    discord = [receiver for receiver in config.get("receivers", [])
               if receiver.get("name") == "discord"]
    require(len(discord) == 1 and len(discord[0].get("discordConfigs", [])) == 1,
            "alertmanager: exactly one native Discord receiver is required")
    require(discord[0]["discordConfigs"][0].get("apiURL") == {
                "name": "alertmanager-discord", "key": "discord-webhook"},
            "alertmanager: Discord webhook must use the ESO SecretKeySelector")
    routes = config["route"].get("routes", [])
    require(len(routes) > 0 and is_drop_route(routes[0], [
                {"name": "application", "matchType": "=~", "value": PREVIEW_SPRING_APP_PATTERN}]),
            "alertmanager: Preview PR app alerts must be dropped before any other route")
    require(len(routes) > 1 and is_drop_route(routes[1], [
                {"name": "job", "matchType": "=", "value": ARGOCD_METRICS_JOB},
                {"name": "name", "matchType": "=~", "value": PREVIEW_ARGOCD_APP_PATTERN}]),
            "alertmanager: Preview PR Argo CD Application alerts must be dropped right after Preview app alerts")
    severity_rules = [rule for rule in config.get("inhibitRules", [])
                      if rule.get("sourceMatch") == [{"name": "severity", "matchType": "=", "value": "critical"}]
                      and rule.get("targetMatch") == [{"name": "severity", "matchType": "=", "value": "warning"}]]
    require(len(severity_rules) == 1
            and {"alertname", "namespace", "name"} <= set(severity_rules[0].get("equal", [])),
            "alertmanager: critical must inhibit warnings only for the same alertname, namespace and name")
    require({"sourceMatch": [{"name": "alertname", "matchType": "=", "value": "ApiHigh5xxErrorRatio"}],
             "targetMatch": [{"name": "alertname", "matchType": "=~",
                              "value": "ApiHigh5xxErrorCount|ApiRouteHigh5xxErrorCount"}],
             "equal": ["application"]} in config.get("inhibitRules", []),
            "alertmanager: 5xx ratio critical must inhibit count-based 5xx warnings per application")

    # Operator 0.92.1's base YAML parser predates webhook_url_file support.
    # The operator alone generates the runtime config from the native resource.
    secret_name = alertmanager["spec"].get("configSecret", f"alertmanager-{ALERTMANAGER_NAME}")
    require(not any(resource.get("kind") == "Secret"
                    and resource.get("metadata", {}).get("name") == secret_name
                    for resource in resources),
            "alertmanager: native global configuration must replace the legacy base Secret")


# main()의 Traefik Pod 모델은 K3s 내장 chart 기본값이다. K3s 버전이나 HelmChartConfig의 metrics 설정이 바뀌면
# traefik PodMonitor가 긁는 port가 달라질 수 있어 모델을 다시 확인하게 한다.
def validate_k3s_traefik_metrics(k3s_version: str, helm_chart_config: dict) -> None:
    require(k3s_version == K3S_TRAEFIK_VERIFIED_VERSION,
            f"K3s {k3s_version}: re-check the bundled Traefik metrics port and labels, then update K3S_TRAEFIK_VERIFIED_VERSION")
    values = yaml.safe_load(helm_chart_config["spec"]["valuesContent"]) or {}
    require("metrics" not in values and "metrics" not in (values.get("ports") or {}),
            "K3s Traefik HelmChartConfig: metrics or ports.metrics override can move the port the traefik PodMonitor scrapes; "
            "re-check it, then update the Traefik Pod model in validate_observability.py")


# TraefikMetricsMissing은 job="traefik"을 찾는다. jobLabel이 Pod의 traefik 값 label이 아니면 job이
# monitoring/traefik이 되어 수집은 되는데 경고가 30분 뒤 잘못 울린다.
def validate_traefik_pod_monitor_job(pod_monitor: dict) -> None:
    spec = pod_monitor.get("spec", {})
    job_label = spec.get("jobLabel")
    require(job_label is not None
            and spec.get("selector", {}).get("matchLabels", {}).get(job_label) == "traefik"
            and K3S_TRAEFIK_POD_LABELS.get(job_label) == "traefik",
            "traefik PodMonitor: jobLabel must name the Pod label whose value is traefik, "
            "because TraefikMetricsMissing matches up{job=\"traefik\"}")


def in_monitor_namespaces(resource: dict, namespace_selector: dict, monitor_namespace: str) -> bool:
    if namespace_selector.get("any"):
        return True
    namespace = resource.get("metadata", {}).get("namespace", "monitoring")
    return namespace in namespace_selector.get("matchNames", [monitor_namespace])


# Prometheus → Monitor → Service(PodMonitor는 Pod)의 selector와 named port가 모두 연결돼야 수집 대상으로 인정한다.
def validate_monitor_selection(prometheus: dict, monitors: list[dict], services: list[dict],
                               workloads: tuple[dict, ...] | list[dict] = ()) -> None:
    spec = prometheus["spec"]
    prefixes = {"ServiceMonitor": "serviceMonitor", "PodMonitor": "podMonitor", "PrometheusRule": "rule"}
    for monitor in monitors:
        kind = monitor.get("kind")
        if kind not in prefixes:
            continue
        metadata = monitor["metadata"]
        identity = f"{kind}/{metadata['name']}"
        namespace = metadata.get("namespace", "monitoring")
        prefix = prefixes[kind]
        require(selector_matches(spec[f"{prefix}Selector"], metadata.get("labels", {}))
                and selector_matches(spec[f"{prefix}NamespaceSelector"],
                                     {"kubernetes.io/metadata.name": namespace}),
                f"{identity}: not selected by Prometheus")
        if kind == "PrometheusRule":
            continue
        monitor_spec = monitor["spec"]
        namespace_selector = monitor_spec.get("namespaceSelector", {})
        if kind == "PodMonitor":
            pods = [pod for workload in workloads
                    if in_monitor_namespaces(workload, namespace_selector, namespace)
                    and (pod := pod_spec(workload)) is not None
                    and selector_matches(monitor_spec["selector"], workload_labels(workload))]
            require(bool(pods), f"{identity}: selector matches no Pod")
            for endpoint in monitor_spec.get("podMetricsEndpoints", []):
                port = endpoint.get("port")
                require(isinstance(port, str) and port != "",
                        f"{identity}: endpoint must reference a named container port")
                require(any(item.get("name") == port for pod in pods
                            for container in pod.get("containers", []) for item in container.get("ports", [])),
                        f"{identity}: endpoint port {port} missing from selected Pod")
            continue
        selected = [service for service in services
                    if in_monitor_namespaces(service, namespace_selector, namespace)
                    and selector_matches(monitor_spec["selector"],
                                         service["metadata"].get("labels", {}))]
        require(bool(selected), f"{identity}: selector matches no Service")
        for endpoint in monitor_spec.get("endpoints", []):
            port = endpoint.get("port")
            require(isinstance(port, str) and port != "",
                    f"{identity}: endpoint must reference a named Service port")
            # Operator relabeling drops Services without the endpoint's named
            # port (e.g. Loki memberlist); at least one eligible Service is required.
            require(any(any(item.get("name") == port for item in service["spec"].get("ports", []))
                        for service in selected),
                    f"{identity}: endpoint port {port} missing from selected Service")


LOKI_OTLP_ENDPOINT = "http://loki.monitoring.svc.cluster.local:3100/otlp"
# chart가 Preview Deployment에 강제하는 SPRING_APPLICATION_NAME(service.name) 형식과 같다.
PREVIEW_SERVICE_NAME = "^preview-umc-product-pr[0-9]+$"
PREVIEW_LOG_ROUTE = {
    "context": "resource",
    "condition": f'IsMatch(attributes["service.name"], "{PREVIEW_SERVICE_NAME}")',
    "pipelines": ["logs/preview"],
}
COLLECTOR_EXPORTERS = {"otlphttp/loki", "otlphttp/loki-preview", "otlphttp/prometheus", "otlphttp/tempo"}
COLLECTOR_PIPELINES = {
    "metrics": (["otlp"], ["memory_limiter", "batch"], ["otlphttp/prometheus"]),
    "traces": (["otlp"], ["memory_limiter", "batch"], ["otlphttp/tempo"]),
    "logs": (["otlp"], ["memory_limiter"], ["routing/logs"]),
    "logs/default": (["routing/logs"], ["memory_limiter", "batch"], ["otlphttp/loki"]),
    "logs/preview": (["routing/logs"],
                     ["memory_limiter", "filter/preview", "probabilistic_sampler/preview", "batch"],
                     ["otlphttp/loki-preview"]),
}


# 수신·전달 주소와 경로 전체를 고정한다. Preview 로그가 운영·dev push에 섞이거나 운영·dev가 샘플링되면 안 된다.
def validate_collector_config(otel: dict) -> None:
    require(set(otel.get("receivers", {})) == {"otlp"}, "OTel: receiver set")
    exporters = otel.get("exporters", {})
    require(set(exporters) == COLLECTOR_EXPORTERS, f"OTel: exporter set must be {sorted(COLLECTOR_EXPORTERS)}")
    require(exporters.get("otlphttp/prometheus", {}).get("endpoint") ==
            f"http://{PROMETHEUS_NAME}.monitoring.svc.cluster.local:9090/api/v1/otlp",
            "OTel: metrics exporter must reach the Prometheus OTLP receiver")
    require(all(exporters.get(name, {}).get("endpoint") == LOKI_OTLP_ENDPOINT
                for name in ("otlphttp/loki", "otlphttp/loki-preview")),
            "OTel: both log exporters must reach the Loki OTLP endpoint")

    pipelines = otel.get("service", {}).get("pipelines", {})
    require(set(pipelines) == set(COLLECTOR_PIPELINES), "OTel: pipeline set")
    for name, (receivers, processors, exporter_names) in COLLECTOR_PIPELINES.items():
        pipeline = pipelines[name]
        require(pipeline.get("receivers") == receivers and pipeline.get("exporters") == exporter_names,
                f"OTel: {name} must keep receivers {receivers} and exporters {exporter_names}")
        require(pipeline.get("processors") == processors,
                f"OTel: {name} processors must be exactly {processors}; "
                "update COLLECTOR_PIPELINES in validate_observability.py to change them")
    routing = otel.get("connectors", {}).get("routing/logs", {})
    require(routing.get("default_pipelines") == ["logs/default"]
            and routing.get("table") == [PREVIEW_LOG_ROUTE],
            "OTel: routing/logs must move only Preview service.name to logs/preview")

    # queue가 꺼지거나 가득 찰 때 기다리면 Preview 재시도가 공유 memory_limiter까지 밀려온다.
    queue = exporters.get("otlphttp/loki-preview", {}).get("sending_queue", {})
    size = queue.get("queue_size")
    require(queue.get("enabled", True) is True, "OTel: Preview Loki sending_queue must stay enabled")
    require(queue.get("block_on_overflow", False) is False,
            "OTel: Preview Loki queue must drop on overflow (block_on_overflow: false)")
    require(queue.get("sizer") == "bytes" and type(size) is int and 0 < size <= 64 << 20,
            "OTel: Preview Loki queue must be bounded in bytes with an integer queue_size up to 64MiB")
    processors = otel.get("processors", {})
    require(processors.get("filter/preview", {}).get("log_conditions")
            == ["log.severity_number < SEVERITY_NUMBER_WARN"],
            "OTel: Preview logs below WARN must be dropped")
    sampler = processors.get("probabilistic_sampler/preview", {})
    percentage = sampler.get("sampling_percentage")
    require(type(percentage) in (int, float) and 0 < percentage < 100,
            "OTel: Preview logs must be sampled below 100%")
    require(sampler.get("fail_closed") is False,
            "OTel: Preview sampler must pass logs without trace (fail_closed: false)")


# 지표·로그·트레이스의 내부 전달 주소를 대조하고 실제 이미지로 검사할 설정을 추출한다.
def validate_runtime_configs(rendered: dict[str, list[dict]]) -> dict[str, str]:
    tempo_raw = configmap_value(rendered["tempo"], "tempo", "tempo.yaml")
    tempo = yaml.safe_load(tempo_raw)
    require(
        set(tempo.get("distributor", {}).get("receivers", {})) == {"otlp"},
        "tempo: only the OTLP receiver may be enabled",
    )
    require(
        set(tempo["distributor"]["receivers"]["otlp"].get("protocols", {}))
        == {"grpc", "http"},
        "tempo: OTLP gRPC/HTTP receiver contract",
    )

    otel_raw = configmap_value(rendered["otel-collector"], "otel-collector", "relay")
    otel = yaml.safe_load(otel_raw)
    validate_collector_config(otel)

    loki_raw = configmap_value(rendered["loki"], "loki", "config.yaml")
    loki = yaml.safe_load(loki_raw)
    validate_loki_query_budget(loki)
    validate_loki_ingestion_limits(loki)
    validate_loki_retention(loki)
    validate_loki_memory(rendered["loki"], loki)
    require(loki.get("ruler", {}).get("alertmanager_url") ==
            f"http://{ALERTMANAGER_NAME}.monitoring.svc.cluster.local:9093",
            "Loki: ruler must send alerts to operator-managed Alertmanager")
    datasources = yaml.safe_load(configmap_value(rendered["grafana"], "grafana", "datasources.yaml"))
    sources = {source.get("uid"): source for source in datasources.get("datasources", [])}
    for uid, service, port in (("prometheus", PROMETHEUS_NAME, 9090),
                               ("alertmanager", ALERTMANAGER_NAME, 9093)):
        require(sources.get(uid, {}).get("url") == f"http://{service}.monitoring.svc.cluster.local:{port}",
                f"Grafana: stable {uid} datasource UID must reach the operator Service")

    validate_prometheus(rendered["prometheus"])
    return {
        "loki-config.yaml": loki_raw,
        "otel-collector-config.yaml": otel_raw,
        "tempo-config.yaml": tempo_raw,
    }


# 실제 Application 렌더에서 권한·자원·수집 연결을 검사하고 후속 스키마/런타임 검증 입력을 만든다.
def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: validate_observability.py OUTPUT_DIR")
    output_dir = Path(sys.argv[1])
    output_dir.mkdir(parents=True, exist_ok=True)

    application_paths = sorted(
        path
        for path in APPLICATION_DIR.glob("*.yaml")
        if path.name not in {"config.yaml", "integrations.yaml"}
    )
    require(
        {path.stem for path in application_paths} == EXPECTED_APPLICATIONS,
        "observability Application set drifted",
    )

    grafana_application = yaml.safe_load(
        (APPLICATION_DIR / "grafana.yaml").read_text(encoding="utf-8")
    )
    grafana_ingress_enabled = validate_grafana_access_contract(grafana_application)
    validate_alertmanager_health_gate()
    validate_preview_application_name(yaml.safe_load(
        (ROOT / "argocd/applications/preview/applicationset.yaml").read_text(encoding="utf-8")))

    prometheus_application = yaml.safe_load(
        (APPLICATION_DIR / "prometheus.yaml").read_text(encoding="utf-8")
    )
    prometheus_source = prometheus_application["spec"]["source"]
    values = prometheus_source["helm"]["valuesObject"]
    require(prometheus_source.get("chart") == "kube-prometheus-stack"
            and str(prometheus_source.get("targetRevision")) == "87.21.0",
            "prometheus: reviewed kube-prometheus-stack chart version")
    require(values.get("grafana", {}).get("enabled") is False
            and values["grafana"].get("forceDeployDashboards") is True,
            "prometheus: default dashboards must feed the existing Grafana")
    require(values.get("prometheusOperator", {}).get("admissionWebhooks", {}).get("enabled") is False,
            "prometheus: admission webhooks require a separate reviewed activation")
    projects = list(yaml.safe_load_all((ROOT / "argocd/projects.yaml").read_text()))
    project = named_resource(projects, "AppProject", "monitoring")["spec"]
    require({(entry["group"], entry["kind"]) for entry in project.get("clusterResourceWhitelist", [])}
            == {("rbac.authorization.k8s.io", "ClusterRole"),
                ("rbac.authorization.k8s.io", "ClusterRoleBinding"),
                ("apiextensions.k8s.io", "CustomResourceDefinition")},
            "monitoring AppProject: only Operator CRDs and explicit cluster RBAC kinds")
    state_metrics = values["kube-state-metrics"]
    require(
        {"nodes", "namespaces", "deployments", "pods", "statefulsets", "persistentvolumeclaims",
         "cronjobs", "jobs"} <= set(state_metrics.get("collectors", []))
        and not ({"secrets", "configmaps"} & set(state_metrics.get("collectors", [])))
        and not state_metrics.get("namespaces")
        and state_metrics.get("rbac", {}).get("create") is True,
        "kube-state-metrics: cluster workload collectors without Secret or ConfigMap access",
    )

    rendered: dict[str, list[dict]] = {}
    pod_labels: list[dict[str, str]] = []
    for path in application_paths:
        name, resources = render_application(path, output_dir)
        rendered[name] = resources
        chart_pod_labels: list[dict[str, str]] = []
        for resource in resources:
            kind = resource["kind"]
            identity = f"{kind}/{resource.get('metadata', {}).get('name', '?')}"
            validate_cluster_resource(name, resource)
            require(
                kind != "Ingress" or (name == "grafana" and grafana_ingress_enabled),
                f"{name}: only the authenticated Grafana Ingress may be public",
            )
            namespace = resource.get("metadata", {}).get("namespace")
            require(namespace in {None, "monitoring"}, f"{name}: {identity} namespace")

            workload = pod_spec(resource)
            if workload is None:
                continue
            labels = workload_labels(resource)
            chart_pod_labels.append(labels)
            pod_labels.append(labels)
            containers = workload.get("initContainers", []) + workload.get("containers", [])
            require(containers, f"{name}: {identity} has no containers")
            for container in containers:
                image = container.get("image", "")
                require(
                    IMAGE_DIGEST.search(image) is not None,
                    f"{name}: {identity}/{container.get('name')} image is not digest-pinned",
                )
                resources_spec = container.get("resources", {})
                requests = resources_spec.get("requests", {})
                limits = resources_spec.get("limits", {})
                require(
                    requests.get("cpu") and requests.get("memory") and limits.get("memory"),
                    f"{name}: {identity}/{container.get('name')} resources are incomplete",
                )

        service_selectors = [
            resource.get("spec", {}).get("selector")
            for resource in resources
            if resource.get("kind") == "Service"
            and resource.get("spec", {}).get("selector")
        ]
        for selector in service_selectors:
            require(
                any(selector_matches({"matchLabels": selector}, labels) for labels in chart_pod_labels),
                f"{name}: Service selector does not match a rendered workload",
            )

    prometheus_resources = rendered["prometheus"]
    validate_grafana_dashboard_startup(rendered["grafana"])
    validate_monitoring_rbac(prometheus_resources)
    arguments = operator_arguments(prometheus_resources)
    require(arguments.get("namespaces") == "monitoring",
            "prometheus-operator: watched namespaces must be monitoring only")
    require(IMAGE_DIGEST.search(arguments.get("prometheus-config-reloader", "")) is not None,
            "prometheus-operator: generated config reloaders must be digest-pinned")
    require(all(arguments.get(argument) not in {None, "0"} for argument in
                ("config-reloader-cpu-request", "config-reloader-memory-request", "config-reloader-memory-limit")),
            "prometheus-operator: generated config reloaders require resources")

    integrations_application = yaml.safe_load((APPLICATION_DIR / "integrations.yaml").read_text())
    require(integrations_application["spec"].get("project") == "monitoring"
            and integrations_application["metadata"].get("annotations", {}).get("argocd.argoproj.io/sync-wave") == "3"
            and integrations_application["spec"]["source"].get("path") == "manifests/observability-integrations",
            "monitoring-integrations: custom resources must follow Operator installation")
    integrations = [resource for path in (ROOT / "manifests/observability-integrations").glob("*.yaml")
                    for resource in yaml.safe_load_all(path.read_text()) if resource]
    require({resource["metadata"]["name"] for resource in integrations
             if resource.get("kind") == "ServiceMonitor"} == {
                "loki", "tempo", "otel-collector", "grafana", "postgres-exporter",
                "cert-manager", "cert-manager-cainjector", "cert-manager-webhook", "external-dns",
                "argocd-application-controller"},
            "monitoring-integrations: required platform scrape targets must be preserved")
    require({resource["metadata"]["name"] for resource in integrations
             if resource.get("kind") == "PodMonitor"} == {"traefik"},
            "monitoring-integrations: Traefik edge 429 dashboard needs the traefik PodMonitor")
    validate_traefik_pod_monitor_job(next(resource for resource in integrations
                                          if resource.get("kind") == "PodMonitor"))
    all_resources = [resource for resources in rendered.values() for resource in resources] + integrations
    write_monitoring_schemas(prometheus_resources, output_dir)
    (output_dir / "monitoring-resources.yaml").write_text(yaml.safe_dump_all(
        resource for resource in all_resources
        if resource.get("apiVersion", "").startswith("monitoring.coreos.com/")), encoding="utf-8")
    external_resources = [resource for path in (ROOT / "manifests").rglob("*.yaml")
                          for resource in yaml.safe_load_all(path.read_text()) if resource]
    for path in output_dir.glob("platform-*.yaml"):
        external_resources.extend(resource for resource in yaml.safe_load_all(path.read_text()) if resource)
    # Argo CD metrics Service는 Ansible bootstrap chart가 만든다. validate_argocd.py 렌더 결과를 쓴다.
    argocd_render = output_dir / "argocd.yaml"
    require(argocd_render.is_file(), "argocd: run validate_argocd.py with the same output directory first")
    argocd_resources = [resource for resource in yaml.safe_load_all(argocd_render.read_text()) if resource]
    external_resources.extend(argocd_resources)
    services = [resource for resource in all_resources + external_resources if resource.get("kind") == "Service"]
    # These Services are produced by Kubernetes/the operator, not Helm. Assert
    # the operator's creation target before modelling its documented port/labels.
    require(arguments.get("kubelet-service") == "monitoring/prometheus-kube-prometheus-kubelet",
            "prometheus-operator: kubelet Service must be created in monitoring")
    services.extend([
        {"kind": "Service", "metadata": {"name": "prometheus-kube-prometheus-kubelet",
         "namespace": "monitoring", "labels": {"app.kubernetes.io/name": "kubelet", "k8s-app": "kubelet"}},
         "spec": {"ports": [{"name": "https-metrics", "port": 10250}]}},
        {"kind": "Service", "metadata": {"name": "kubernetes", "namespace": "default",
         "labels": {"component": "apiserver", "provider": "kubernetes"}},
         "spec": {"ports": [{"name": "https", "port": 443}]}},
        # K3s installs this Service; its metrics port was verified on the IDC.
        {"kind": "Service", "metadata": {"name": "kube-dns", "namespace": "kube-system",
         "labels": {"k8s-app": "kube-dns"}},
         "spec": {"ports": [{"name": "metrics", "port": 9153}]}},
    ])
    # K3s가 설치하는 Traefik Pod다. 내장 traefik chart 40.1.4+up40.1.0이 ports.metrics(9100)를 이름 metrics로 연다.
    validate_k3s_traefik_metrics(
        yaml.safe_load((ROOT / "ansible/roles/k3s/defaults/main.yml").read_text())["k3s_version"],
        yaml.safe_load((ROOT / "ansible/roles/k3s/templates/traefik-config.yaml.j2").read_text()))
    traefik_pod = {"kind": "Pod", "metadata": {"name": "traefik", "namespace": "kube-system",
                   "labels": K3S_TRAEFIK_POD_LABELS},
                   "spec": {"containers": [{"name": "traefik", "ports": [{"name": "metrics", "containerPort": 9100}]}]}}
    prometheus = named_resource(prometheus_resources, "Prometheus", PROMETHEUS_NAME)
    validate_monitor_selection(prometheus, all_resources, services,
                               all_resources + external_resources + [traefik_pod])
    rules = [resource for resource in all_resources if resource.get("kind") == "PrometheusRule"]
    require(any(resource in integrations for resource in rules)
            and any(resource in prometheus_resources for resource in rules),
            "prometheus: both UMC and chart rules must be present")
    # promtool understands rule groups, not Kubernetes resource wrappers.
    for rule in rules:
        (output_dir / f"prometheus-rules-{rule['metadata']['name']}.yaml").write_text(
            yaml.safe_dump(rule["spec"], sort_keys=False), encoding="utf-8")

    for filename, content in validate_runtime_configs(rendered).items():
        (output_dir / filename).write_text(content, encoding="utf-8")

    with (ROOT / "manifests" / "observability" / "networkpolicy.yaml").open(
        encoding="utf-8"
    ) as stream:
        policies = [item for item in yaml.safe_load_all(stream) if item]
    for policy in policies:
        selector = policy["spec"].get("podSelector", {})
        require(
            any(selector_matches(selector, labels) for labels in pod_labels),
            f"NetworkPolicy/{policy['metadata']['name']} selects no rendered workload",
        )
    prometheus_labels = workload_labels(prometheus)
    require(admits_prometheus_ingress(external_resources,
                                      named_resource(external_resources, "Deployment", "postgres-exporter"),
                                      prometheus_labels, {"metrics", 9187}),
            "PostgreSQL exporter NetworkPolicy must admit operator-managed Prometheus Pods on the metrics port")
    require(admits_prometheus_ingress(argocd_resources,
                                      named_resource(argocd_resources, "StatefulSet", "argocd-application-controller"),
                                      prometheus_labels, {"metrics", 8082}),
            "Argo CD application-controller NetworkPolicy must admit Prometheus on the metrics port")
    # Preview drop route의 job matcher가 실제 metrics Service 이름을 가리켜야 한다.
    named_resource(argocd_resources, "Service", ARGOCD_METRICS_JOB)
    tempo_policy = next(
        policy
        for policy in policies
        if policy["metadata"]["name"] == "tempo-allow-required-ingress"
    )
    tempo_ports = {
        port["port"]
        for rule in tempo_policy["spec"].get("ingress", [])
        for port in rule.get("ports", [])
    }
    require(
        tempo_ports == {3100, 4318},
        "Tempo NetworkPolicy must expose only query/metrics and OTLP/HTTP",
    )
    require(
        len(tempo_policy["spec"].get("ingress", [])) == 1
        and tempo_policy["spec"]["ingress"][0].get("from")
        == [{"podSelector": {}}],
        "Tempo NetworkPolicy source must be monitoring namespace Pods only",
    )
    tempo_labels = next(
        resource["spec"]["template"]["metadata"]["labels"]
        for resource in rendered["tempo"]
        if resource.get("kind") == "StatefulSet"
    )
    effective_tempo_ports: set[int] = set()
    for policy in policies:
        if not selector_matches(policy["spec"].get("podSelector", {}), tempo_labels):
            continue
        for rule in policy["spec"].get("ingress", []):
            require(
                rule.get("ports"),
                f"NetworkPolicy/{policy['metadata']['name']} opens every Tempo port",
            )
            effective_tempo_ports.update(port["port"] for port in rule["ports"])
    require(
        effective_tempo_ports == {3100, 4318},
        "combined NetworkPolicies expose unexpected Tempo ports",
    )

    grafana_policy = next(
        policy
        for policy in policies
        if policy["metadata"]["name"] == "grafana-allow-traefik-ingress"
    )
    require(
        grafana_policy["spec"].get("ingress")
        == [
            {
                "from": [
                    {
                        "namespaceSelector": {
                            "matchLabels": {
                                "kubernetes.io/metadata.name": "kube-system"
                            }
                        },
                        "podSelector": {"matchLabels": K3S_TRAEFIK_POD_LABELS},
                    }
                ],
                "ports": [{"protocol": "TCP", "port": 3000}],
            }
        ],
        "Grafana NetworkPolicy must allow only Traefik on TCP/3000",
    )

    print(
        "observability charts are renderable and satisfy namespace, image, resource, "
        "selector, ingress, and Prometheus discovery contracts"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

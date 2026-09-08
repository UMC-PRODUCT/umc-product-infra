#!/usr/bin/env python3
"""Render pinned observability charts and validate their shared runtime contract."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
APPLICATION_DIR = ROOT / "argocd" / "applications" / "platform" / "observability"
EXPECTED_APPLICATIONS = {"grafana", "loki", "otel-collector", "prometheus", "tempo"}
PROMETHEUS_NAME = "prometheus-kube-prometheus-prometheus"
ALERTMANAGER_NAME = "prometheus-kube-prometheus-alertmanager"
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


class ManifestLoader(yaml.SafeLoader):
    """Accept the literal '=' enum in Operator CRDs (YAML 1.1 value tag)."""


ManifestLoader.add_constructor("tag:yaml.org,2002:value", ManifestLoader.construct_scalar)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"observability validation failed: {message}")


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


def write_monitoring_schemas(resources: list[dict], output_dir: Path) -> None:
    """Use the pinned chart's CRDs for kubeconform instead of a moving registry."""
    def strict_schema(value):
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
    completed = subprocess.run(
        command,
        input=yaml.safe_dump(source.get("helm", {}).get("valuesObject", {})),
        text=True,
        capture_output=True,
        check=False,
    )
    require(completed.returncode == 0, f"{name}: Helm render failed\n{completed.stderr}")

    output_path = output_dir / f"observability-{name}.yaml"
    output_path.write_text(completed.stdout, encoding="utf-8")
    resources = [
        document
        for document in yaml.load_all(completed.stdout, Loader=ManifestLoader)
        if isinstance(document, dict) and document.get("kind")
    ]
    require(resources, f"{name}: chart rendered no resources")
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


def validate_grafana_dashboard_startup(resources: list[dict]) -> None:
    workload = pod_spec(named_resource(resources, "Deployment", "grafana"))
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


def validate_alertmanager_health_gate() -> None:
    config = yaml.safe_load((ROOT / "manifests/cluster/argocd-health.yaml").read_text())
    health = config.get("data", {}).get("resource.customizations.health.monitoring.coreos.com_Alertmanager", "")
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

    # Operator 0.92.1's base YAML parser predates webhook_url_file support.
    # The operator alone generates the runtime config from the native resource.
    secret_name = alertmanager["spec"].get("configSecret", f"alertmanager-{ALERTMANAGER_NAME}")
    require(not any(resource.get("kind") == "Secret"
                    and resource.get("metadata", {}).get("name") == secret_name
                    for resource in resources),
            "alertmanager: native global configuration must replace the legacy base Secret")


def validate_monitor_selection(prometheus: dict, monitors: list[dict], services: list[dict]) -> None:
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
        if kind != "ServiceMonitor":
            continue
        monitor_spec = monitor["spec"]
        namespace_selector = monitor_spec.get("namespaceSelector", {})
        selected_namespaces = namespace_selector.get("matchNames", [namespace])
        selected = [service for service in services
                    if (namespace_selector.get("any") or
                        service.get("metadata", {}).get("namespace", "monitoring") in selected_namespaces)
                    and selector_matches(monitor_spec["selector"],
                                         service["metadata"].get("labels", {}))]
        require(selected, f"{identity}: selector matches no Service")
        for endpoint in monitor_spec.get("endpoints", []):
            port = endpoint.get("port")
            require(isinstance(port, str) and port,
                    f"{identity}: endpoint must reference a named Service port")
            # Operator relabeling drops Services without the endpoint's named
            # port (e.g. Loki memberlist); at least one eligible Service is required.
            require(any(any(item.get("name") == port for item in service["spec"].get("ports", []))
                        for service in selected),
                    f"{identity}: endpoint port {port} missing from selected Service")


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
    require(set(otel.get("receivers", {})) == {"otlp"}, "OTel: receiver set")
    require(
        set(otel.get("exporters", {}))
        == {"otlphttp/loki", "otlphttp/prometheus", "otlphttp/tempo"},
        "OTel: exporter set",
    )
    require(otel["exporters"]["otlphttp/prometheus"].get("endpoint") ==
            f"http://{PROMETHEUS_NAME}.monitoring.svc.cluster.local:9090/api/v1/otlp",
            "OTel: metrics exporter must reach the Prometheus OTLP receiver")
    require(
        set(otel.get("service", {}).get("pipelines", {}))
        == {"logs", "metrics", "traces"},
        "OTel: pipeline set",
    )

    loki_raw = configmap_value(rendered["loki"], "loki", "config.yaml")
    loki = yaml.safe_load(loki_raw)
    require(
        str(loki.get("limits_config", {}).get("retention_period")) == "2160h",
        "Loki: retention must remain 90 days",
    )
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
                "cert-manager", "cert-manager-cainjector", "cert-manager-webhook", "external-dns"},
            "monitoring-integrations: required platform scrape targets must be preserved")
    all_resources = [resource for resources in rendered.values() for resource in resources] + integrations
    write_monitoring_schemas(prometheus_resources, output_dir)
    (output_dir / "monitoring-resources.yaml").write_text(yaml.safe_dump_all(
        resource for resource in all_resources
        if resource.get("apiVersion", "").startswith("monitoring.coreos.com/")), encoding="utf-8")
    external_resources = [resource for path in (ROOT / "manifests").rglob("*.yaml")
                          for resource in yaml.safe_load_all(path.read_text()) if resource]
    for path in output_dir.glob("platform-*.yaml"):
        external_resources.extend(resource for resource in yaml.safe_load_all(path.read_text()) if resource)
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
    prometheus = named_resource(prometheus_resources, "Prometheus", PROMETHEUS_NAME)
    validate_monitor_selection(prometheus, all_resources, services)
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
    db_policies = [resource for resource in external_resources if resource.get("kind") == "NetworkPolicy"
                   and resource.get("metadata", {}).get("name") == "postgres-exporter-allow-prometheus-ingress"]
    require(len(db_policies) == 1, "PostgreSQL exporter must restrict Prometheus ingress")
    peers = db_policies[0]["spec"]["ingress"][0]["from"]
    require(any(selector_matches(peer.get("podSelector", {}), prometheus_labels)
                and selector_matches(peer.get("namespaceSelector", {}),
                                     {"kubernetes.io/metadata.name": "monitoring"})
                for peer in peers), "PostgreSQL exporter NetworkPolicy must select operator-managed Prometheus Pods")
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
                        "podSelector": {
                            "matchLabels": {
                                "app.kubernetes.io/name": "traefik",
                                "app.kubernetes.io/instance": "traefik-kube-system",
                            }
                        },
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

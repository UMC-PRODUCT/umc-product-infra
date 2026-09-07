#!/usr/bin/env python3
"""Render pinned observability charts and validate their shared runtime contract."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
APPLICATION_DIR = ROOT / "argocd" / "applications" / "platform" / "observability"
EXPECTED_APPLICATIONS = {"grafana", "loki", "otel-collector", "prometheus", "tempo"}
EXPECTED_PROMETHEUS_TARGETS = {
    "alertmanager": ["prometheus-alertmanager.monitoring.svc.cluster.local:9093"],
    "cert-manager": ["cert-manager.cert-manager.svc.cluster.local:9402"],
    "cert-manager-cainjector": [
        "cert-manager-cainjector.cert-manager.svc.cluster.local:9402"
    ],
    "cert-manager-webhook": [
        "cert-manager-webhook.cert-manager.svc.cluster.local:9402"
    ],
    "external-dns": ["external-dns.external-dns.svc.cluster.local:7979"],
    "grafana": ["grafana.monitoring.svc.cluster.local:80"],
    "kube-state-metrics": [
        "prometheus-kube-state-metrics.monitoring.svc.cluster.local:8080"
    ],
    "loki": ["loki.monitoring.svc.cluster.local:3100"],
    "node-exporter": ["node-exporter.monitoring.svc.cluster.local:9100"],
    "otel-collector": ["otel-collector.monitoring.svc.cluster.local:8888"],
    "postgres-exporter": ["postgres-exporter.db.svc.cluster.local:9187"],
    "prometheus": ["localhost:9090"],
    "tempo": ["tempo.monitoring.svc.cluster.local:3100"],
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
    return None


def selector_matches(selector: dict, labels: dict[str, str]) -> bool:
    for key, value in selector.get("matchLabels", {}).items():
        if labels.get(key) != value:
            return False
    for expression in selector.get("matchExpressions", []):
        key = expression["key"]
        operator = expression["operator"]
        values = expression.get("values", [])
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
        for document in yaml.safe_load_all(completed.stdout)
        if isinstance(document, dict) and document.get("kind")
    ]
    require(resources, f"{name}: chart rendered no resources")
    return name, resources


def validate_grafana_access_contract(application: dict) -> bool:
    values = application["spec"]["source"]["helm"]["valuesObject"]
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
        == {"default_home_dashboard_path": "/tmp/dashboards/system-overview.json"},
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


def validate_prometheus(resources: list[dict]) -> str:
    raw_config = configmap_value(resources, "prometheus-server", "prometheus.yml")
    config = yaml.safe_load(raw_config)
    jobs = config.get("scrape_configs", [])
    targets = {
        job.get("job_name"): [
            target
            for static_config in job.get("static_configs", [])
            for target in static_config.get("targets", [])
        ]
        for job in jobs
    }
    require(
        targets == EXPECTED_PROMETHEUS_TARGETS,
        "prometheus: static scrape targets drifted",
    )
    postgres_exporter = next(
        job for job in jobs if job.get("job_name") == "postgres-exporter"
    )
    require(
        postgres_exporter.get("scrape_timeout") == "10s"
        and postgres_exporter.get("static_configs")
        == [
            {
                "targets": ["postgres-exporter.db.svc.cluster.local:9187"],
                "labels": {"environment": "prod"},
            }
        ],
        "prometheus: PostgreSQL exporter scrape contract",
    )
    require(
        "kubernetes_sd_configs" not in yaml.safe_dump(config),
        "prometheus: Kubernetes service discovery requires forbidden API credentials",
    )
    require(
        config.get("alerting", {}).get("alertmanagers")
        == [
            {
                "static_configs": [
                    {
                        "targets": [
                            "prometheus-alertmanager.monitoring.svc.cluster.local:9093"
                        ]
                    }
                ]
            }
        ],
        "prometheus: Alertmanager must use the static cluster-local Service",
    )

    server = next(
        resource
        for resource in resources
        if resource.get("kind") == "Deployment"
        and resource.get("metadata", {}).get("name") == "prometheus-server"
    )
    require(
        server["spec"]["template"]["spec"].get("automountServiceAccountToken") is False,
        "prometheus: API token must not be mounted",
    )
    return raw_config


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

    return {
        "alertmanager-config.yaml": configmap_value(
            rendered["prometheus"], "prometheus-alertmanager", "alertmanager.yml"
        ),
        "loki-config.yaml": loki_raw,
        "otel-collector-config.yaml": otel_raw,
        "prometheus-config.yaml": validate_prometheus(rendered["prometheus"]),
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
        if path.name != "config.yaml"
    )
    require(
        {path.stem for path in application_paths} == EXPECTED_APPLICATIONS,
        "observability Application set drifted",
    )

    grafana_application = yaml.safe_load(
        (APPLICATION_DIR / "grafana.yaml").read_text(encoding="utf-8")
    )
    grafana_ingress_enabled = validate_grafana_access_contract(grafana_application)

    prometheus_application = yaml.safe_load(
        (APPLICATION_DIR / "prometheus.yaml").read_text(encoding="utf-8")
    )
    state_metrics = prometheus_application["spec"]["source"]["helm"]["valuesObject"][
        "kube-state-metrics"
    ]
    require(
        state_metrics.get("collectors")
        == ["cronjobs", "jobs", "pods", "statefulsets", "persistentvolumeclaims"]
        and state_metrics.get("namespaces") == ["db"]
        and state_metrics.get("rbac") == {"create": False},
        "kube-state-metrics: DB-only collectors and external least-privilege RBAC",
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
            require(kind not in CLUSTER_SCOPED_KINDS, f"{name}: cluster resource {identity}")
            require(
                kind != "Ingress" or (name == "grafana" and grafana_ingress_enabled),
                f"{name}: only the authenticated Grafana Ingress may be public",
            )
            namespace = resource.get("metadata", {}).get("namespace")
            require(namespace in {None, "monitoring"}, f"{name}: {identity} namespace")

            workload = pod_spec(resource)
            if workload is None:
                continue
            labels = (
                resource.get("spec", {})
                .get("template", {})
                .get("metadata", {})
                .get("labels", {})
            )
            if kind == "CronJob":
                labels = (
                    resource["spec"]["jobTemplate"]["spec"]["template"]
                    .get("metadata", {})
                    .get("labels", {})
                )
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

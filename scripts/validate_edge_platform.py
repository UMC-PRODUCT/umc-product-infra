#!/usr/bin/env python3
"""Render and validate cert-manager, ExternalDNS, and Reloader contracts."""

from __future__ import annotations

import ipaddress
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
APPLICATION_DIR = ROOT / "argocd" / "applications" / "platform"
IMAGE_DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
EXPECTED = {
    "cert-manager": {
        "project": "cert-manager",
        "namespace": "cert-manager",
        "repo": "https://charts.jetstack.io",
        "chart": "cert-manager",
        "version": "v1.21.1",
        "images": {
            "quay.io/jetstack/cert-manager-cainjector:v1.21.1@sha256:ccf6b919ec0500745a47a910118f834f9636d0aac1ff221245cd2557ed8c7c98",
            "quay.io/jetstack/cert-manager-controller:v1.21.1@sha256:416a2d76870d996460e62bd7f521bf14fa017be9e3e904aab92163a331fcb61a",
            "quay.io/jetstack/cert-manager-webhook:v1.21.1@sha256:d8b3961b51c8c7320633f8208dc46bf88aa13804d0f7cbe48a096b2c523cee42",
            "quay.io/jetstack/cert-manager-startupapicheck:v1.21.1@sha256:d8ab6416e6e7303a86fa0a8daa82c94a8001f21c9d78eb2e7db20534e5d07ae8",
        },
    },
    "external-dns": {
        "project": "external-dns",
        "namespace": "external-dns",
        "repo": "https://kubernetes-sigs.github.io/external-dns/",
        "chart": "external-dns",
        "version": "1.21.1",
        "images": {
            "registry.k8s.io/external-dns/external-dns:v0.21.0@sha256:f53faaf71cb270d1ca9dce6ea0c94bfebf1a18696263487f0fbc74b9bf2bd7ff"
        },
    },
    "reloader": {
        "project": "reloader",
        "namespace": "reloader",
        "repo": "https://stakater.github.io/stakater-charts",
        "chart": "reloader",
        "version": "2.2.16",
        "images": {
            "ghcr.io/stakater/reloader@sha256:"
            "b253579350a835082cdad8d8736671cedaa0f8309437c894bd1bf1c2f0e0d45e"
        },
    },
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"edge platform validation failed: {message}")


def documents(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as stream:
        return [item for item in yaml.safe_load_all(stream) if isinstance(item, dict)]


def pod_spec(resource: dict) -> dict | None:
    if resource.get("kind") in {"Deployment", "Job"}:
        return resource.get("spec", {}).get("template", {}).get("spec")
    return None


def render(name: str, output_dir: Path) -> tuple[dict, list[dict]]:
    application = documents(APPLICATION_DIR / f"{name}.yaml")[0]
    expected = EXPECTED[name]
    source = application["spec"]["source"]
    require(application.get("kind") == "Application", f"{name}: kind")
    require(application["spec"]["project"] == expected["project"], f"{name}: project")
    require(
        application["spec"]["destination"]["namespace"] == expected["namespace"],
        f"{name}: namespace",
    )
    require(source.get("repoURL") == expected["repo"], f"{name}: chart repository")
    require(source.get("chart") == expected["chart"], f"{name}: chart")
    require(str(source.get("targetRevision")) == expected["version"], f"{name}: version")

    release_name = source.get("helm", {}).get("releaseName", name)
    command = [
        "helm",
        "template",
        release_name,
        source["chart"],
        "--repo",
        source["repoURL"],
        "--version",
        str(source["targetRevision"]),
        "--namespace",
        expected["namespace"],
        "--kube-version",
        "1.36.0",
        "-f",
        "-",
    ]
    completed = subprocess.run(
        command,
        input=yaml.safe_dump(source["helm"]["valuesObject"]),
        text=True,
        capture_output=True,
        check=False,
    )
    require(completed.returncode == 0, f"{name}: Helm render failed\n{completed.stderr}")
    (output_dir / f"platform-{name}.yaml").write_text(completed.stdout, encoding="utf-8")
    resources = [
        item
        for item in yaml.safe_load_all(completed.stdout)
        if isinstance(item, dict) and item.get("kind")
    ]
    require(resources, f"{name}: rendered no resources")
    return application, resources


def validate_workloads(name: str, resources: list[dict]) -> None:
    actual_images: set[str] = set()
    for resource in resources:
        workload = pod_spec(resource)
        if workload is None:
            continue
        require(
            workload.get("securityContext", {}).get("runAsNonRoot") is True,
            f"{name}: {resource['kind']}/{resource['metadata']['name']} must run as non-root",
        )
        require(
            workload.get("securityContext", {}).get("seccompProfile", {}).get("type")
            == "RuntimeDefault",
            f"{name}: {resource['kind']}/{resource['metadata']['name']} seccomp",
        )
        containers = workload.get("initContainers", []) + workload.get("containers", [])
        require(containers, f"{name}: workload without containers")
        for container in containers:
            image = container.get("image", "")
            actual_images.add(image)
            require(IMAGE_DIGEST.search(image) is not None, f"{name}: unpinned image {image}")
            security = container.get("securityContext", {})
            require(security.get("allowPrivilegeEscalation") is False, f"{name}: escalation")
            require(security.get("readOnlyRootFilesystem") is True, f"{name}: root filesystem")
            require(
                security.get("capabilities", {}).get("drop") == ["ALL"],
                f"{name}: capabilities",
            )
            resources_spec = container.get("resources", {})
            require(
                resources_spec.get("requests", {}).get("cpu")
                and resources_spec.get("requests", {}).get("memory")
                and resources_spec.get("limits", {}).get("memory"),
                f"{name}: incomplete resources for {container.get('name')}",
            )
    require(actual_images == EXPECTED[name]["images"], f"{name}: image set drifted")


def validate_cert_manager(resources: list[dict]) -> None:
    deployments = {
        item["metadata"]["name"]: item
        for item in resources
        if item.get("kind") == "Deployment"
    }
    require(
        set(deployments) == {"cert-manager", "cert-manager-cainjector", "cert-manager-webhook"},
        "cert-manager: deployment set",
    )
    controller = deployments["cert-manager"]["spec"]["template"]["spec"]["containers"][0]
    args = set(controller.get("args", []))
    require("--enable-certificate-owner-ref=true" in args, "cert-manager: Certificate ownerRef")
    require("--max-concurrent-challenges=5" in args, "cert-manager: challenge budget")
    solver_args = [arg for arg in args if arg.startswith("--acme-http01-solver-image=")]
    require(len(solver_args) == 1 and IMAGE_DIGEST.search(solver_args[0]), "cert-manager: solver image")
    require(
        sum(item.get("kind") == "CustomResourceDefinition" for item in resources) == 6,
        "cert-manager: CRD set",
    )


def validate_external_dns(application: dict, resources: list[dict]) -> tuple[str, str]:
    deployments = [item for item in resources if item.get("kind") == "Deployment"]
    require(len(deployments) == 1, "external-dns: one Deployment")
    container = deployments[0]["spec"]["template"]["spec"]["containers"][0]
    args = set(container.get("args", []))
    required_args = {
        "--source=ingress",
        "--policy=sync",
        "--registry=txt",
        "--txt-owner-id=umc-infra-idc",
        "--txt-prefix=_external-dns.",
        "--domain-filter=university.neordinary.com",
        "--annotation-filter=external-dns.kubernetes.io/managed-by=umc-infra",
        "--provider=aws",
        "--ingress-class=traefik",
        "--aws-zone-type=public",
        "--managed-record-types=A",
        "--managed-record-types=TXT",
        "--events",
    }
    require(required_args <= args, f"external-dns: arguments missing {required_args - args}")
    require(
        {arg for arg in args if arg.startswith("--provider=")} == {"--provider=aws"},
        "external-dns: AWS must be the only DNS provider",
    )
    filters = [arg.removeprefix("--target-net-filter=") for arg in args if arg.startswith("--target-net-filter=")]
    require(len(filters) == 1, "external-dns: exactly one target network filter")
    zone_filters = [
        arg.removeprefix("--zone-id-filter=")
        for arg in args
        if arg.startswith("--zone-id-filter=")
    ]
    require(len(zone_filters) == 1, "external-dns: exactly one Route53 Hosted Zone ID")
    require(
        zone_filters[0] == "bootstrap-required"
        or re.fullmatch(r"Z[A-Z0-9]+", zone_filters[0]) is not None,
        "external-dns: Route53 Hosted Zone ID format",
    )
    expected_args = required_args | {
        "--log-level=info",
        "--log-format=text",
        "--interval=1m",
        f"--target-net-filter={filters[0]}",
        f"--zone-id-filter={zone_filters[0]}",
    }
    require(args == expected_args, "external-dns: exact AWS Route53 argument set")
    env = {item["name"]: item for item in container.get("env", [])}
    require(
        env
        == {
            "AWS_DEFAULT_REGION": {
                "name": "AWS_DEFAULT_REGION",
                "value": "ap-northeast-2",
            },
            "AWS_ACCESS_KEY_ID": {
                "name": "AWS_ACCESS_KEY_ID",
                "valueFrom": {
                    "secretKeyRef": {
                        "name": "route53-credentials",
                        "key": "access-key-id",
                    }
                },
            },
            "AWS_SECRET_ACCESS_KEY": {
                "name": "AWS_SECRET_ACCESS_KEY",
                "valueFrom": {
                    "secretKeyRef": {
                        "name": "route53-credentials",
                        "key": "secret-access-key",
                    }
                },
            },
        },
        "external-dns: Route53 region and credential Secret",
    )
    require(
        application["spec"]["syncPolicy"]["automated"].get("prune") is True,
        "external-dns: exact-record deletion requires prune",
    )
    require(
        application["spec"]["source"]["helm"]["valuesObject"].get("managedRecordTypes")
        == ["A", "TXT"],
        "external-dns: only A records and TXT ownership may be managed",
    )
    return filters[0], zone_filters[0]


def validate_reloader(application: dict, resources: list[dict]) -> None:
    values = application["spec"]["source"]["helm"]["valuesObject"]
    reloader_values = values["reloader"]
    require(values.get("fullnameOverride") == "reloader", "reloader: stable resource names")
    require(reloader_values.get("watchGlobally") is False, "reloader: global watch disabled")
    require(
        reloader_values.get("namespaces") == ["app", "dev-app", "preview"],
        "reloader: application namespace allowlist",
    )
    require(reloader_values.get("autoReloadAll") is False, "reloader: workloads must opt in")
    require(reloader_values.get("ignoreSecrets") is False, "reloader: Secret watch enabled")
    require(reloader_values.get("ignoreConfigMaps") is True, "reloader: ConfigMaps ignored")
    require(reloader_values.get("ignoreJobs") is True, "reloader: Jobs ignored")
    require(reloader_values.get("ignoreCronJobs") is True, "reloader: CronJobs ignored")
    require(reloader_values.get("reloadOnCreate") is False, "reloader: create reload disabled")
    require(reloader_values.get("reloadOnDelete") is False, "reloader: delete reload disabled")
    require(reloader_values.get("syncAfterRestart") is False, "reloader: restart sync disabled")
    require(reloader_values.get("enableHA") is False, "reloader: single controller replica")
    require(
        reloader_values.get("reloadStrategy") == "annotations",
        "reloader: Argo-compatible reload strategy",
    )

    require(
        not any(
            item.get("kind") in {"ClusterRole", "ClusterRoleBinding"}
            for item in resources
        ),
        "reloader: cluster-wide RBAC is forbidden",
    )
    deployments = [item for item in resources if item.get("kind") == "Deployment"]
    require(len(deployments) == 1, "reloader: one controller Deployment")
    deployment = deployments[0]
    require(
        deployment["metadata"].get("name") == "reloader"
        and deployment["metadata"].get("namespace") == "reloader",
        "reloader: controller identity",
    )
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    require(
        container.get("args", [])
        == [
            "--log-level=info",
            "--resources-to-ignore=configmaps",
            "--ignored-workload-types=jobs,cronjobs",
            "--namespaces=app,dev-app,preview,reloader",
            "--reload-strategy=annotations",
        ],
        "reloader: controller arguments",
    )

    service_accounts = [
        item for item in resources if item.get("kind") == "ServiceAccount"
    ]
    require(
        len(service_accounts) == 1
        and service_accounts[0]["metadata"].get("name") == "reloader"
        and service_accounts[0]["metadata"].get("namespace") == "reloader"
        and deployment["spec"]["template"]["spec"].get("serviceAccountName")
        == "reloader",
        "reloader: controller ServiceAccount",
    )

    role_namespaces = Counter(
        item["metadata"]["namespace"]
        for item in resources
        if item.get("kind") == "Role"
    )
    binding_namespaces = Counter(
        item["metadata"]["namespace"]
        for item in resources
        if item.get("kind") == "RoleBinding"
    )
    expected_namespaces = Counter(
        {"app": 1, "dev-app": 1, "preview": 1, "reloader": 2}
    )
    require(role_namespaces == expected_namespaces, "reloader: namespace-scoped Roles")
    require(
        binding_namespaces == expected_namespaces,
        "reloader: namespace-scoped RoleBindings",
    )
    roles = {
        (item["metadata"]["namespace"], item["metadata"]["name"]): item
        for item in resources
        if item.get("kind") == "Role"
    }
    expected_main_rules = [
        {"apiGroups": [""], "resources": ["secrets"], "verbs": ["list", "get", "watch"]},
        {
            "apiGroups": ["apps"],
            "resources": ["deployments", "daemonsets", "statefulsets"],
            "verbs": ["list", "get", "update", "patch"],
        },
        {"apiGroups": ["batch"], "resources": ["cronjobs"], "verbs": ["list", "get"]},
        {
            "apiGroups": ["batch"],
            "resources": ["jobs"],
            "verbs": ["create", "delete", "list", "get"],
        },
        {"apiGroups": [""], "resources": ["events"], "verbs": ["create", "patch"]},
    ]
    for namespace in ("app", "dev-app", "preview", "reloader"):
        require(
            roles[(namespace, "reloader-role")].get("rules") == expected_main_rules,
            f"reloader: {namespace} Role rules drifted",
        )
    require(
        roles[("reloader", "reloader-metadata-role")].get("rules")
        == [
            {
                "apiGroups": [""],
                "resources": ["configmaps"],
                "verbs": ["list", "get", "watch", "create", "update"],
            }
        ],
        "reloader: metadata Role rules drifted",
    )

    bindings = [item for item in resources if item.get("kind") == "RoleBinding"]
    expected_subjects = [
        {"kind": "ServiceAccount", "name": "reloader", "namespace": "reloader"}
    ]
    for binding in bindings:
        role_name = binding["metadata"]["name"].removesuffix("-binding")
        require(binding.get("subjects") == expected_subjects, "reloader: RoleBinding subject")
        require(
            binding.get("roleRef")
            == {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "Role",
                "name": role_name,
            },
            "reloader: RoleBinding reference",
        )
    network_policies = [
        item for item in resources if item.get("kind") == "NetworkPolicy"
    ]
    require(
        len(network_policies) == 1
        and network_policies[0]["metadata"].get("namespace") == "reloader"
        and network_policies[0]["spec"].get("policyTypes") == ["Ingress", "Egress"],
        "reloader: controller NetworkPolicy",
    )
    network_policy_spec = network_policies[0]["spec"]
    require(
        network_policy_spec.get("ingress")
        == [
            {
                "ports": [{"port": "http"}],
                "from": [
                    {
                        "namespaceSelector": {
                            "matchLabels": {
                                "kubernetes.io/metadata.name": "monitoring"
                            }
                        }
                    }
                ],
            }
        ]
        and network_policy_spec.get("egress") == [{"ports": [{"port": 443}]}],
        "reloader: controller network paths",
    )
    require(
        application["spec"]["syncPolicy"].get("syncOptions")
        == ["FailOnSharedResource=true"],
        "reloader: shared resource conflict protection",
    )
    require(
        application["spec"]["syncPolicy"].get("automated")
        == {"selfHeal": True, "prune": True},
        "reloader: automated reconciliation",
    )


def validate_issuers(zone_filter: str) -> None:
    issuers = documents(ROOT / "manifests" / "cert-manager" / "clusterissuers.yaml")
    require(
        {item.get("metadata", {}).get("name") for item in issuers}
        == {"letsencrypt-staging", "letsencrypt-production"},
        "ClusterIssuer set",
    )
    for issuer in issuers:
        acme = issuer["spec"]["acme"]
        require(acme["server"].startswith("https://acme"), "ClusterIssuer ACME endpoint")
        require(len(acme.get("solvers", [])) == 1, "ClusterIssuer solver count")
        solver = acme["solvers"][0]
        require(
            solver["selector"]["dnsZones"] == ["university.neordinary.com"],
            "ClusterIssuer zone",
        )
        require(
            solver["dns01"]
            == {
                "route53": {
                    "region": "ap-northeast-2",
                    "hostedZoneID": zone_filter,
                    "accessKeyIDSecretRef": {
                        "name": "route53-credentials",
                        "key": "access-key-id",
                    },
                    "secretAccessKeySecretRef": {
                        "name": "route53-credentials",
                        "key": "secret-access-key",
                    },
                }
            },
            "ClusterIssuer Route53 solver",
        )


def validate_preview_wildcard_certificate() -> None:
    certificate_path = (
        ROOT / "manifests" / "cert-manager" / "preview-wildcard-certificate.yaml"
    )
    require(certificate_path.is_file(), "preview wildcard Certificate manifest")
    file_certificates = [
        item for item in documents(certificate_path) if item.get("kind") == "Certificate"
    ]
    require(len(file_certificates) == 1, "preview: one wildcard Certificate in manifest")
    all_certificates = [
        item
        for path in sorted((ROOT / "manifests" / "cert-manager").glob("*.yaml"))
        for item in documents(path)
        if item.get("kind") == "Certificate"
    ]
    require(
        len(all_certificates) == 1,
        "preview: cert-manager manifests must own one central Certificate",
    )

    certificate = file_certificates[0]
    require(certificate.get("apiVersion") == "cert-manager.io/v1", "preview: Certificate API")
    metadata = certificate["metadata"]
    require(metadata.get("name") == "preview-wildcard", "preview: Certificate name")
    require(metadata.get("namespace") == "preview", "preview: Certificate namespace")
    require(
        metadata.get("annotations", {}).get("argocd.argoproj.io/sync-wave") == "1",
        "preview: Certificate must follow ClusterIssuers",
    )
    spec = certificate["spec"]
    require(
        spec.get("dnsNames") == ["*.university.neordinary.com"],
        "preview: wildcard SAN",
    )
    require(
        spec.get("secretName") == "preview-wildcard-tls",
        "preview: shared wildcard TLS Secret",
    )
    require(
        spec.get("issuerRef")
        == {
            "group": "cert-manager.io",
            "kind": "ClusterIssuer",
            "name": "letsencrypt-staging",
        },
        "preview: bootstrap staging ClusterIssuer",
    )
    require(
        spec.get("privateKey")
        == {"algorithm": "ECDSA", "size": 256, "rotationPolicy": "Always"},
        "preview: wildcard private key contract",
    )


def validate_target_gate(target_filter: str, zone_filter: str) -> None:
    base = yaml.safe_load((ROOT / "charts" / "umc-product-server" / "values.yaml").read_text())
    grafana = documents(
        ROOT / "argocd" / "applications" / "platform" / "observability" / "grafana.yaml"
    )[0]
    grafana_ingress = grafana["spec"]["source"]["helm"]["valuesObject"]["ingress"]
    app_target = base["externalDNS"]["target"]
    app_issuer = base["certificate"]["issuerRef"]["name"]
    grafana_target = grafana_ingress["annotations"]["external-dns.kubernetes.io/target"]
    grafana_certificate = next(
        item
        for item in grafana["spec"]["source"]["helm"]["valuesObject"]["extraObjects"]
        if item.get("kind") == "Certificate"
    )
    grafana_issuer = grafana_certificate["spec"]["issuerRef"]["name"]

    if app_target == "bootstrap-required":
        require(app_issuer == "letsencrypt-staging", "bootstrap app issuer must be staging")
        require(grafana_issuer == "letsencrypt-staging", "bootstrap Grafana issuer must be staging")
        require(
            zone_filter == "bootstrap-required"
            or re.fullmatch(r"Z[A-Z0-9]+", zone_filter) is not None,
            "bootstrap Zone ID must be a placeholder or an existing Route53 zone",
        )
        require(target_filter == "192.0.2.0/32", "bootstrap target filter must fail closed")
        require(grafana_target == "192.0.2.1", "Grafana bootstrap target must be filtered")
        require(grafana_ingress.get("enabled") is False, "Grafana placeholder Ingress")
        for filename in ("values-prod.yaml", "values-dev.yaml", "values-preview.yaml"):
            values = yaml.safe_load((ROOT / "charts" / "umc-product-server" / filename).read_text())
            require(values["ingress"]["enabled"] is False, f"{filename}: placeholder Ingress")
        return

    try:
        address = ipaddress.ip_address(app_target)
    except ValueError as error:
        raise SystemExit(f"edge platform validation failed: invalid IDC address: {error}") from error
    require(address.version == 4 and address.is_global, "IDC target must be a public IPv4")
    require(app_issuer == "letsencrypt-production", "active app issuer must be production")
    require(grafana_issuer == "letsencrypt-production", "active Grafana issuer must be production")
    require(
        re.fullmatch(r"Z[A-Z0-9]+", zone_filter) is not None,
        "Route53 Hosted Zone ID must start with Z and contain uppercase alphanumerics",
    )
    require(target_filter == f"{address}/32", "ExternalDNS filter must match IDC IPv4")
    require(grafana_target == str(address), "Grafana target must match IDC IPv4")
    require(grafana_ingress.get("enabled") is True, "active Grafana Ingress must be enabled")


def validate_project_boundaries() -> None:
    projects = {
        item["metadata"]["name"]: item
        for item in documents(ROOT / "argocd" / "projects.yaml")
        if item.get("kind") == "AppProject"
    }
    expected = {
        "cert-manager": {
            ("admissionregistration.k8s.io", "MutatingWebhookConfiguration"),
            ("admissionregistration.k8s.io", "ValidatingWebhookConfiguration"),
            ("apiextensions.k8s.io", "CustomResourceDefinition"),
            ("cert-manager.io", "ClusterIssuer"),
            ("rbac.authorization.k8s.io", "ClusterRole"),
            ("rbac.authorization.k8s.io", "ClusterRoleBinding"),
        },
        "external-dns": {
            ("rbac.authorization.k8s.io", "ClusterRole"),
            ("rbac.authorization.k8s.io", "ClusterRoleBinding"),
        },
        "reloader": set(),
    }
    expected_namespaced = {
        "cert-manager": {
            ("", "Service"),
            ("", "ServiceAccount"),
            ("apps", "Deployment"),
            ("batch", "Job"),
            ("cert-manager.io", "Certificate"),
            ("rbac.authorization.k8s.io", "Role"),
            ("rbac.authorization.k8s.io", "RoleBinding"),
        },
        "external-dns": {
            ("", "Service"),
            ("", "ServiceAccount"),
            ("apps", "Deployment"),
        },
        "reloader": {
            ("", "ServiceAccount"),
            ("apps", "Deployment"),
            ("networking.k8s.io", "NetworkPolicy"),
            ("rbac.authorization.k8s.io", "Role"),
            ("rbac.authorization.k8s.io", "RoleBinding"),
        },
    }
    expected_destinations = {
        "cert-manager": {
            ("https://kubernetes.default.svc", "cert-manager"),
            ("https://kubernetes.default.svc", "preview"),
        },
        "external-dns": {
            ("https://kubernetes.default.svc", "external-dns"),
        },
        "reloader": {
            ("https://kubernetes.default.svc", "reloader"),
            ("https://kubernetes.default.svc", "app"),
            ("https://kubernetes.default.svc", "dev-app"),
            ("https://kubernetes.default.svc", "preview"),
        },
    }
    expected_source_repos = {
        "cert-manager": {
            "https://charts.jetstack.io",
            "https://github.com/UMC-PRODUCT/umc-product-infra.git",
        },
        "external-dns": {"https://kubernetes-sigs.github.io/external-dns/"},
        "reloader": {"https://stakater.github.io/stakater-charts"},
    }
    for name, whitelist in expected.items():
        require(
            set(projects[name]["spec"].get("sourceRepos", []))
            == expected_source_repos[name],
            f"{name}: AppProject source repositories",
        )
        destinations = {
            (item["server"], item["namespace"])
            for item in projects[name]["spec"].get("destinations", [])
        }
        require(destinations == expected_destinations[name], f"{name}: AppProject destinations")
        actual = {
            (item["group"], item["kind"])
            for item in projects[name]["spec"].get("clusterResourceWhitelist", [])
        }
        require(actual == whitelist, f"{name}: AppProject cluster whitelist")
        actual_namespaced = {
            (item["group"], item["kind"])
            for item in projects[name]["spec"].get("namespaceResourceWhitelist", [])
        }
        require(
            actual_namespaced == expected_namespaced[name],
            f"{name}: AppProject namespace whitelist",
        )


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: validate_edge_platform.py OUTPUT_DIR")
    output_dir = Path(sys.argv[1])
    output_dir.mkdir(parents=True, exist_ok=True)

    rendered: dict[str, list[dict]] = {}
    applications: dict[str, dict] = {}
    for name in EXPECTED:
        applications[name], rendered[name] = render(name, output_dir)
        validate_workloads(name, rendered[name])
        require(
            not any(item.get("kind") == "Ingress" for item in rendered[name]),
            f"{name}: controller chart must not expose an Ingress",
        )

    validate_cert_manager(rendered["cert-manager"])
    target_filter, zone_filter = validate_external_dns(
        applications["external-dns"], rendered["external-dns"]
    )
    validate_reloader(applications["reloader"], rendered["reloader"])
    validate_issuers(zone_filter)
    validate_preview_wildcard_certificate()
    validate_target_gate(target_filter, zone_filter)
    validate_project_boundaries()
    print("cert-manager, ExternalDNS, and Reloader platform contracts are valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())

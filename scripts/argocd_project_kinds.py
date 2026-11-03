#!/usr/bin/env python3
"""Group every resource the repo's Argo CD Applications render by AppProject.

usage: argocd_project_kinds.py VALIDATION_OUTPUT_DIR

Prints the minimal cluster/namespace resource whitelist per project and every kind
that the current projects.yaml would reject. Run scripts/validate.sh with
VALIDATION_OUTPUT_DIR first so the chart renders exist.
"""

from __future__ import annotations

import fnmatch
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
APPLICATION_SOURCES = ("bootstrap", "argocd/applications")
PROJECTS_PATH = ROOT / "argocd" / "projects.yaml"

# chart source는 validate.sh가 남긴 렌더 파일로 대응한다. directory source는 Git 경로를 직접 읽는다.
# gate를 연 합성 렌더까지 포함해 Ingress·Certificate를 연 뒤 필요한 kind도 미리 허용 범위에 넣는다.
CHART_RENDERS = {
    "umc-product-server": ("prod-desired-state.yaml", "prod.yaml", "active-edge-test-net.yaml"),
    "dev-umc-product-server": (
        "dev-desired-state.yaml", "dev.yaml",
        "dev-test-api-ingress-disabled.yaml", "dev-test-api-deployment-disabled.yaml",
    ),
    "umc-product-server-preview": (
        "preview-desired-state.yaml", "preview.yaml", "active-preview-edge-test-net.yaml",
        "preview-pr-42-desired-state.yaml", "preview-pr-43-desired-state.yaml",
    ),
    "secrets": ("secrets.yaml",),
    "cert-manager": ("platform-cert-manager.yaml",),
    "external-dns": ("platform-external-dns.yaml",),
    "reloader": ("platform-reloader.yaml",),
    "grafana": ("observability-grafana.yaml",),
    "loki": ("observability-loki.yaml",),
    "otel-collector": ("observability-otel-collector.yaml",),
    "prometheus": ("observability-prometheus.yaml",),
    "tempo": ("observability-tempo.yaml",),
}

# Kubernetes API에서 namespace가 없는 kind다. 여기에 없는 kind는 namespaced로 판정한다.
CLUSTER_SCOPED = {
    ("", "Namespace"), ("", "PersistentVolume"), ("", "Node"),
    ("admissionregistration.k8s.io", "MutatingWebhookConfiguration"),
    ("admissionregistration.k8s.io", "ValidatingWebhookConfiguration"),
    ("admissionregistration.k8s.io", "ValidatingAdmissionPolicy"),
    ("admissionregistration.k8s.io", "ValidatingAdmissionPolicyBinding"),
    ("apiextensions.k8s.io", "CustomResourceDefinition"),
    ("apiregistration.k8s.io", "APIService"),
    ("cert-manager.io", "ClusterIssuer"),
    ("external-secrets.io", "ClusterSecretStore"),
    ("external-secrets.io", "ClusterExternalSecret"),
    ("networking.k8s.io", "IngressClass"),
    ("rbac.authorization.k8s.io", "ClusterRole"),
    ("rbac.authorization.k8s.io", "ClusterRoleBinding"),
    ("scheduling.k8s.io", "PriorityClass"),
    ("storage.k8s.io", "StorageClass"),
}

GroupKind = tuple[str, str]


@dataclass(frozen=True)
class Application:
    name: str
    project: str
    renders: tuple[Path, ...]


@dataclass(frozen=True)
class Project:
    name: str
    cluster_whitelist: tuple[GroupKind, ...]
    namespace_whitelist: tuple[GroupKind, ...] | None

    def permits(self, kind: GroupKind) -> bool:
        if kind in CLUSTER_SCOPED:
            return matches(self.cluster_whitelist, kind)
        # Argo CD는 namespaceResourceWhitelist가 없거나 비면 모든 namespaced kind를 허용한다.
        return not self.namespace_whitelist or matches(self.namespace_whitelist, kind)


def matches(whitelist: tuple[GroupKind, ...], kind: GroupKind) -> bool:
    return any(fnmatch.fnmatchcase(kind[0], group) and fnmatch.fnmatchcase(kind[1], name)
               for group, name in whitelist)


def group_kind(resource: dict) -> GroupKind:
    api_version = resource.get("apiVersion", "")
    return (api_version.split("/")[0] if "/" in api_version else "", resource["kind"])


# kube-prometheus-stack CRD 렌더의 `=` enum 값을 문자열로 읽는다.
class ManifestLoader(yaml.SafeLoader):
    pass


ManifestLoader.add_constructor("tag:yaml.org,2002:value", ManifestLoader.construct_scalar)


def load_yaml(path: Path) -> list[dict]:
    return [item for item in yaml.load_all(path.read_text(encoding="utf-8"), Loader=ManifestLoader)
            if isinstance(item, dict)]


def directory_renders(source: dict) -> tuple[Path, ...]:
    directory = ROOT / source["path"]
    if (directory / "Chart.yaml").exists():
        return ()
    options = source.get("directory", {})
    paths = directory.rglob("*.yaml") if options.get("recurse") else directory.glob("*.yaml")
    excludes = options.get("exclude", "").strip("{}").split(",")
    return tuple(sorted(path for path in paths
                        if not any(pattern and fnmatch.fnmatch(str(path.relative_to(directory)), pattern)
                                   for pattern in excludes)))


def applications(output_dir: Path) -> list[Application]:
    found = []
    for source_dir in APPLICATION_SOURCES:
        for path in sorted((ROOT / source_dir).rglob("*.yaml")):
            for item in load_yaml(path):
                if item.get("kind") not in {"Application", "ApplicationSet"}:
                    continue
                name = item["metadata"]["name"]
                spec = item["spec"] if item["kind"] == "Application" else item["spec"]["template"]["spec"]
                source = spec["source"]
                renders = tuple(output_dir / render for render in CHART_RENDERS.get(name, ()))
                if not renders and "chart" not in source:
                    renders = directory_renders(source)
                found.append(Application(name, spec["project"], renders))
    return found


def projects() -> dict[str, Project]:
    def kinds(entries: list[dict] | None) -> tuple[GroupKind, ...]:
        return tuple((entry["group"], entry["kind"]) for entry in entries or ())

    return {
        item["metadata"]["name"]: Project(
            item["metadata"]["name"],
            kinds(item["spec"].get("clusterResourceWhitelist")),
            kinds(item["spec"]["namespaceResourceWhitelist"])
            if "namespaceResourceWhitelist" in item["spec"] else None,
        )
        for item in load_yaml(PROJECTS_PATH)
        if item.get("kind") == "AppProject"
    }


def required_kinds(apps: list[Application]) -> tuple[dict[str, dict[GroupKind, set[str]]], list[str]]:
    """Return {project: {group_kind: app names}} and the renders that do not exist."""
    needed: dict[str, dict[GroupKind, set[str]]] = {}
    missing = []
    for app in apps:
        needed.setdefault(app.project, {})
        if not app.renders:
            missing.append(f"{app.name}: no render registered")
        for render in app.renders:
            if not render.exists():
                missing.append(f"{app.name}: {render}")
                continue
            for resource in load_yaml(render):
                needed[app.project].setdefault(group_kind(resource), set()).add(app.name)
    return needed, missing


def rejected_kinds(needed: dict[str, dict[GroupKind, set[str]]],
                   reviewed: dict[str, Project]) -> list[str]:
    errors = []
    for project_name, kinds in sorted(needed.items()):
        project = reviewed.get(project_name)
        if project is None:
            errors.append(f"{project_name}: AppProject is not declared")
            continue
        for kind in sorted(kinds):
            if not project.permits(kind):
                scope = "cluster" if kind in CLUSTER_SCOPED else "namespace"
                errors.append(f"{project_name}: {scope} kind {kind[0] or 'core'}/{kind[1]} "
                              f"from {', '.join(sorted(kinds[kind]))} is not whitelisted")
    return errors


# kind 단위 whitelist로는 이름을 제한할 수 없다. platform이 argocd에 둘 수 있는 객체를 이름까지 고정한다.
PLATFORM_ARGOCD_OBJECTS = {
    ("SecretStore", "aws-secretsmanager", None),
    ("ExternalSecret", "preview-github-token", "preview-github-token"),
}


def platform_argocd_errors(apps: list[Application]) -> list[str]:
    errors = []
    for app in apps:
        if app.project != "platform":
            continue
        for render in app.renders:
            for resource in load_yaml(render) if render.exists() else ():
                if resource["metadata"].get("namespace") != "argocd":
                    continue
                target = resource.get("spec", {}).get("target", {}).get("name") \
                    if resource["kind"] == "ExternalSecret" else None
                if (resource["kind"], resource["metadata"]["name"], target) not in PLATFORM_ARGOCD_OBJECTS:
                    errors.append(f"platform: {resource['kind']}/{resource['metadata']['name']} "
                                  f"(target {target}) may not be written into argocd")
    return errors


def validation_errors(output_dir: Path) -> list[str]:
    """Errors that make an Application sync fail or widen platform's reach into argocd."""
    apps = applications(output_dir)
    needed, missing = required_kinds(apps)
    reviewed = projects()
    restricted = {name for name, project in reviewed.items() if project.namespace_whitelist}
    unproven = [line for line in missing
                if next(app.project for app in apps if line.startswith(f"{app.name}:")) in restricted]
    return ([f"missing render: {line}" for line in unproven]
            + rejected_kinds(needed, reviewed) + platform_argocd_errors(apps))


def main(output_dir: Path) -> int:
    apps = applications(output_dir)
    needed, missing = required_kinds(apps)
    reviewed = projects()
    for project_name, kinds in sorted(needed.items()):
        print(f"## {project_name}")
        for scope, is_cluster in (("clusterResourceWhitelist", True), ("namespaceResourceWhitelist", False)):
            print(f"  {scope}:")
            for kind in sorted(kind for kind in kinds if (kind in CLUSTER_SCOPED) == is_cluster):
                print(f"    - group: {kind[0]!r}\n      kind: {kind[1]}"
                      f"  # {', '.join(sorted(kinds[kind]))}")
    for line in missing:
        print(f"missing render: {line}")
    errors = rejected_kinds(needed, reviewed) + platform_argocd_errors(apps)
    for line in errors:
        print(f"rejected: {line}")
    return 1 if errors else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: argocd_project_kinds.py VALIDATION_OUTPUT_DIR")
    raise SystemExit(main(Path(sys.argv[1])))

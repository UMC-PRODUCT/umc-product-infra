#!/usr/bin/env python3
"""Group every resource the repo's Argo CD Applications render by AppProject.

usage: argocd_project_kinds.py VALIDATION_OUTPUT_DIR

Prints the minimal cluster/namespace resource whitelist per project, then every error:
a rendered kind the project would reject, a namespaced whitelist entry that no render
needs and NAMESPACE_WHITELIST_EXTRAS does not justify, a missing render, or a platform
object in argocd outside the pinned set. Exits 1 on any error. scripts/validate.sh
calls it after the chart renders exist.
"""

from __future__ import annotations

import fnmatch
import json
import re
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
# group, kind, name. 빈 name은 같은 group/kind의 모든 이름을 뜻한다(Argo CD ClusterResourceRestrictionItem).
Restriction = tuple[str, str, str]

# 렌더에 없지만 열어 두는 namespaced kind다. 항목마다 이유를 적는다. 여기 없는 kind를 whitelist에 넣으면 실패한다.
NAMESPACE_WHITELIST_EXTRAS: dict[str, dict[GroupKind, str]] = {}

# Argo CD 디렉터리 source가 읽는 확장자다. jsonnet은 이 검사가 평가하지 못하므로 거부한다.
MANIFEST_SUFFIXES = (".yaml", ".yml", ".json")
SKIP_RENDERING_MARKER = "+argocd:skip-file-rendering"


@dataclass(frozen=True)
class Application:
    name: str
    project: str
    renders: tuple[Path, ...]


@dataclass(frozen=True)
class Rendered:
    app: str
    kind: GroupKind
    resource: dict

    @property
    def name(self) -> str:
        return self.resource.get("metadata", {}).get("name", "")


@dataclass(frozen=True)
class Project:
    name: str
    cluster_whitelist: tuple[Restriction, ...]
    cluster_blacklist: tuple[Restriction, ...]
    namespace_whitelist: tuple[GroupKind, ...] | None
    namespace_blacklist: tuple[GroupKind, ...]

    # Argo CD v3.5 AppProject.IsGroupKindNamePermitted와 같은 판정이다.
    # namespaced whitelist는 필드가 없을 때만 전부 허용하고 []이면 전부 거부한다. cluster whitelist는 비면 전부 거부한다.
    def permits(self, kind: GroupKind, name: str) -> bool:
        if kind in CLUSTER_SCOPED:
            return (named_match(self.cluster_whitelist, kind, name)
                    and not named_match(self.cluster_blacklist, kind, name))
        allowed = self.namespace_whitelist is None or matches(self.namespace_whitelist, kind)
        return allowed and not matches(self.namespace_blacklist, kind)


def matches(entries: tuple[GroupKind, ...], kind: GroupKind) -> bool:
    return any(fnmatch.fnmatchcase(kind[0], group) and fnmatch.fnmatchcase(kind[1], name)
               for group, name in entries)


def named_match(entries: tuple[Restriction, ...], kind: GroupKind, name: str) -> bool:
    return any(matches(((group, kind_pattern),), kind) and (not name_pattern or fnmatch.fnmatchcase(name, name_pattern))
               for group, kind_pattern, name_pattern in entries)


def is_pattern(value: str) -> bool:
    return any(char in value for char in "*?[")


def group_kind(resource: dict) -> GroupKind:
    api_version = resource.get("apiVersion", "")
    return (api_version.split("/")[0] if "/" in api_version else "", resource["kind"])


# kube-prometheus-stack CRD 렌더의 `=` enum 값을 문자열로 읽는다.
class ManifestLoader(yaml.SafeLoader):
    pass


ManifestLoader.add_constructor("tag:yaml.org,2002:value", ManifestLoader.construct_scalar)


def load_yaml(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        loaded = json.loads(text)
        items = loaded if isinstance(loaded, list) else [loaded]
    else:
        items = yaml.load_all(text, Loader=ManifestLoader)
    return [item for item in items if isinstance(item, dict)]


# Argo CD glob처럼 `*`가 `/`도 넘고 `{a,b}` 대안을 지원한다.
def glob_match(pattern: str, path: str) -> bool:
    brace = re.search(r"\{([^{}]*)\}", pattern)
    if brace is None:
        return fnmatch.fnmatchcase(path, pattern)
    return any(glob_match(pattern[:brace.start()] + option + pattern[brace.end():], path)
               for option in brace.group(1).split(","))


def directory_renders(source: dict) -> tuple[Path, ...]:
    directory = ROOT / source["path"]
    if (directory / "Chart.yaml").exists():
        return ()
    options = source.get("directory", {})
    include, exclude = options.get("include", ""), options.get("exclude", "")
    found = []
    for path in sorted(directory.rglob("*") if options.get("recurse") else directory.glob("*")):
        relative = str(path.relative_to(directory))
        if not path.is_file() or (exclude and glob_match(exclude, relative)) \
                or (include and not glob_match(include, relative)):
            continue
        if path.suffix == ".jsonnet":
            raise SystemExit(f"{path}: jsonnet directory sources are not evaluated by this check")
        if path.suffix in MANIFEST_SUFFIXES and SKIP_RENDERING_MARKER not in path.read_text(encoding="utf-8"):
            found.append(path)
    return tuple(found)


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

    def restrictions(entries: list[dict] | None) -> tuple[Restriction, ...]:
        return tuple((entry["group"], entry["kind"], entry.get("name", "")) for entry in entries or ())

    return {
        item["metadata"]["name"]: Project(
            item["metadata"]["name"],
            restrictions(item["spec"].get("clusterResourceWhitelist")),
            restrictions(item["spec"].get("clusterResourceBlacklist")),
            kinds(item["spec"]["namespaceResourceWhitelist"])
            if item["spec"].get("namespaceResourceWhitelist") is not None else None,
            kinds(item["spec"].get("namespaceResourceBlacklist")),
        )
        for item in load_yaml(PROJECTS_PATH)
        if item.get("kind") == "AppProject"
    }


def rendered_resources(apps: list[Application]) -> tuple[dict[str, list[Rendered]], list[tuple[Application, str]]]:
    """Return {project: rendered resources} and (Application, reason) for every missing render."""
    rendered: dict[str, list[Rendered]] = {}
    missing = []
    for app in apps:
        resources = rendered.setdefault(app.project, [])
        absent = [str(render) for render in app.renders if not render.exists()]
        if not app.renders or absent:
            missing.append((app, ", ".join(absent) or "no render registered"))
        for render in app.renders:
            resources.extend(Rendered(app.name, group_kind(item), item)
                             for item in (load_yaml(render) if render.exists() else ()))
    return rendered, missing


def kinds_by_app(resources: list[Rendered]) -> dict[GroupKind, set[str]]:
    by_kind: dict[GroupKind, set[str]] = {}
    for item in resources:
        by_kind.setdefault(item.kind, set()).add(item.app)
    return by_kind


def label(kind: GroupKind) -> str:
    return f"{kind[0] or 'core'}/{kind[1]}"


def rejected_kinds(rendered: dict[str, list[Rendered]], reviewed: dict[str, Project]) -> list[str]:
    errors = []
    for project_name, resources in sorted(rendered.items()):
        project = reviewed.get(project_name)
        if project is None:
            errors.append(f"{project_name}: AppProject is not declared")
            continue
        rejected = kinds_by_app([item for item in resources if not project.permits(item.kind, item.name)])
        for kind, apps in sorted(rejected.items()):
            scope = "cluster" if kind in CLUSTER_SCOPED else "namespace"
            errors.append(f"{project_name}: {scope} kind {label(kind)} "
                          f"from {', '.join(sorted(apps))} is not whitelisted")
    return errors


# 렌더가 쓰지 않는 kind를 whitelist에 더하면 Secret·RBAC 같은 권한이 조용히 넓어진다.
# 선언 = 렌더에 필요한 namespaced kind ∪ NAMESPACE_WHITELIST_EXTRAS여야 한다.
def whitelist_drift_errors(rendered: dict[str, list[Rendered]], reviewed: dict[str, Project]) -> list[str]:
    errors = []
    for project_name, project in sorted(reviewed.items()):
        if project.namespace_whitelist is None:
            continue
        declared = set(project.namespace_whitelist)
        extras = set(NAMESPACE_WHITELIST_EXTRAS.get(project_name, {}))
        required = {item.kind for item in rendered.get(project_name, []) if item.kind not in CLUSTER_SCOPED}
        for kind in sorted(kind for kind in declared if is_pattern(kind[0]) or is_pattern(kind[1])):
            errors.append(f"{project_name}: namespace whitelist entry {label(kind)} must name an exact group/kind")
        for kind in sorted(declared - required - extras):
            errors.append(f"{project_name}: namespace kind {label(kind)} is whitelisted but no Application renders it; "
                          "remove it or justify it in NAMESPACE_WHITELIST_EXTRAS")
        for kind in sorted(extras - declared):
            errors.append(f"{project_name}: NAMESPACE_WHITELIST_EXTRAS lists {label(kind)}, which is not whitelisted")
        for kind in sorted(extras & required):
            errors.append(f"{project_name}: NAMESPACE_WHITELIST_EXTRAS lists {label(kind)}, which is already rendered")
    for project_name in sorted(set(NAMESPACE_WHITELIST_EXTRAS) - set(reviewed)):
        errors.append(f"{project_name}: NAMESPACE_WHITELIST_EXTRAS names an undeclared AppProject")
    return errors


# kind 단위 whitelist로는 이름을 제한할 수 없다. platform이 argocd에 둘 수 있는 객체를 이름까지 고정한다.
PLATFORM_ARGOCD_OBJECTS = {
    ("SecretStore", "aws-secretsmanager", None),
    ("ExternalSecret", "preview-github-token", "preview-github-token"),
}
# argocd.argoproj.io/secret-type 등이 붙은 Secret은 Argo CD가 저장소·cluster 자격증명으로 읽는다.
ARGOCD_LABEL_PREFIX = "argocd.argoproj.io/"


def platform_argocd_errors(rendered: dict[str, list[Rendered]]) -> list[str]:
    errors = []
    for item in rendered.get("platform", []):
        resource = item.resource
        if resource["metadata"].get("namespace") != "argocd":
            continue
        target = resource.get("spec", {}).get("target", {}) if resource["kind"] == "ExternalSecret" else {}
        if (resource["kind"], item.name, target.get("name")) not in PLATFORM_ARGOCD_OBJECTS:
            errors.append(f"platform: {resource['kind']}/{item.name} "
                          f"(target {target.get('name')}) may not be written into argocd")
        labels = (target.get("template") or {}).get("metadata", {}).get("labels") or {}
        for key in sorted(key for key in labels if key.startswith(ARGOCD_LABEL_PREFIX)):
            errors.append(f"platform: {resource['kind']}/{item.name} may not label its Secret with {key}")
    return errors


def validation_errors(output_dir: Path) -> list[str]:
    """Errors that make an Application sync fail, widen a project, or widen platform's reach into argocd."""
    apps = applications(output_dir)
    rendered, missing = rendered_resources(apps)
    reviewed = projects()
    # namespaced whitelist가 있는 project는 렌더 없이 필요한 kind를 증명할 수 없다.
    return ([f"missing render: {app.name}: {reason}" for app, reason in missing
             if app.project in reviewed and reviewed[app.project].namespace_whitelist is not None]
            + rejected_kinds(rendered, reviewed)
            + whitelist_drift_errors(rendered, reviewed)
            + platform_argocd_errors(rendered))


def main(output_dir: Path) -> int:
    rendered, missing = rendered_resources(applications(output_dir))
    for project_name, resources in sorted(rendered.items()):
        by_kind = kinds_by_app(resources)
        print(f"## {project_name}")
        for scope, is_cluster in (("clusterResourceWhitelist", True), ("namespaceResourceWhitelist", False)):
            print(f"  {scope}:")
            for kind in sorted(kind for kind in by_kind if (kind in CLUSTER_SCOPED) == is_cluster):
                print(f"    - group: {kind[0]!r}\n      kind: {kind[1]}"
                      f"  # {', '.join(sorted(by_kind[kind]))}")
    for app, reason in missing:
        print(f"render not checked: {app.name} ({app.project}): {reason}")
    errors = validation_errors(output_dir)
    for line in errors:
        print(f"error: {line}")
    print(f"AppProject whitelist check: {len(errors)} error(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: argocd_project_kinds.py VALIDATION_OUTPUT_DIR")
    raise SystemExit(main(Path(sys.argv[1])))

#!/usr/bin/env python3
"""관측성 원본을 Kubernetes ConfigMap/PrometheusRule로 결정적으로 변환한다.

원본은 이 저장소의 ``observability/``가 소유한다. 배포가 검증된 초기 허용
목록만 ``manifests/observability/``에 생성하며, PrometheusRule은 CRD 이후
적용할 ``manifests/observability-integrations/``에 생성한다. 허용 목록에서
빠진 dashboard 원본은 보관하더라도 Grafana sidecar에 전달하지 않는다.

    python3 scripts/gen-configmaps.py
    python3 scripts/gen-configmaps.py --check
    python3 scripts/gen-configmaps.py /path/to/observability
"""

from __future__ import annotations

import json
import pathlib
import re
import sys


HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
OUTPUT = ROOT / "manifests"
DASHBOARD_DIRECTORY = pathlib.Path("observability/dashboards")

UMC_PRODUCT_FOLDER = "UMC Product"
# chart dashboard가 들어가는 folder다.
KUBERNETES_FOLDER = "Kubernetes"
# 배포할 dashboard 원본 → Grafana folder.
DASHBOARD_ALLOWLIST = {
    "api-flow.json": UMC_PRODUCT_FOLDER,
    "api-lifecycle.json": UMC_PRODUCT_FOLDER,
    "cache.json": UMC_PRODUCT_FOLDER,
    "kubernetes-guide.json": KUBERNETES_FOLDER,
    "node-exporter-host.json": UMC_PRODUCT_FOLDER,
    "postgresql-detail.json": UMC_PRODUCT_FOLDER,
    "server-default.json": UMC_PRODUCT_FOLDER,
    "server-logs.json": UMC_PRODUCT_FOLDER,
    "system-overview.json": UMC_PRODUCT_FOLDER,
}
PROMETHEUS_RULE_SOURCE = "prometheus-alerts.yaml"
LOKI_RULE_SOURCE = "loki-alerts.yaml"
LOKI_RULE_KEY = "default-log-alerts.yml"

ALERT_START = re.compile(r"^(?P<indent> *)- alert: (?P<name>[^ ]+)\s*$")
GROUP_NAME = re.compile(r"^  - name: (?P<name>\S+)\s*$", re.MULTILINE)

# 현재 k3s에 target이 없거나 kube-prometheus-stack 기본 규칙과 중복된다.
# 원본 변경으로 이름이 사라지면 조용히 건너뛰지 않고 생성 단계가 실패한다.
K3S_DROPPED_ALERTS = frozenset(
    {
        "ApiServerDown",
        "PrometheusTargetDown",
        "RedisDown",
        "RedisMemoryHighUsage",
        "RedisRejectedConnections",
        "SSLCertificateExpiresSoon",
        "ExternalHttpHealthCheckFailed",
        "ApiHealthCheckFailed",
        # KPS TargetDown와 Prometheus/Alertmanager 기본 규칙이 담당한다.
        "PrometheusDown",
        "AlertmanagerDown",
        # KPS node-exporter/Kubernetes 규칙이 담당한다. local-path 노드의
        # 절대 여유 용량(10Gi) 경보인 LowDiskFreeSpace는 별도로 유지한다.
        "HighCpuUsage",
        "HighMemoryUsage",
        "HighDiskUsage",
        "ContainerRestarted",
    }
)


def read_text(path: pathlib.Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"필수 관측성 원본이 없습니다: {path}")
    return path.read_text(encoding="utf-8")


def block_scalar(text: str, indent: int) -> str:
    pad = " " * indent
    return "\n".join((pad + line).rstrip() for line in text.splitlines())


def configmap(name: str, filename: str, content: str, *, folder: str | None = None) -> str:
    dashboard_label = '    grafana_dashboard: "1"\n' if folder else ""
    dashboard_folder = (
        "  annotations:\n"
        f"    grafana_folder: /tmp/dashboards/{folder}\n"
        if folder else ""
    )
    return (
        "# 자동 생성 — observability/ 원본은 직접 수정하고 scripts/gen-configmaps.py를 재실행하세요.\n"
        "apiVersion: v1\n"
        "kind: ConfigMap\n"
        "metadata:\n"
        f"  name: {name}\n"
        "  namespace: monitoring\n"
        "  labels:\n"
        "    app.kubernetes.io/managed-by: umc-infra\n"
        f"{dashboard_label}"
        f"{dashboard_folder}"
        "data:\n"
        f"  {filename}: |\n"
        f"{block_scalar(content, 4)}\n"
    )


def prometheus_rule(content: str) -> str:
    return (
        "# 자동 생성 — observability/ 원본은 직접 수정하고 scripts/gen-configmaps.py를 재실행하세요.\n"
        "apiVersion: monitoring.coreos.com/v1\n"
        "kind: PrometheusRule\n"
        "metadata:\n"
        "  name: prometheus-alert-rules\n"
        "  namespace: monitoring\n"
        "  labels:\n"
        "    app.kubernetes.io/managed-by: umc-infra\n"
        "    release: prometheus\n"
        "spec:\n"
        f"{block_scalar(content, 2)}\n"
    )


# UMC 식별자와 현재 K3s 지표 계약을 확인한 뒤 정렬된 JSON으로 생성물 차이를 안정화한다.
def canonical_dashboard(filename: str, content: str) -> str:
    try:
        document = json.loads(content)
    except json.JSONDecodeError as error:
        raise ValueError(f"유효하지 않은 Grafana JSON입니다: {filename}: {error}") from error

    if "spec" in document:
        identifier = document.get("metadata", {}).get("name", "")
        tags = document.get("spec", {}).get("tags", [])
    else:
        identifier = document.get("uid", "")
        tags = document.get("tags", [])

    if not identifier.startswith("umc-product-"):
        raise ValueError(f"UMC dashboard 식별자가 아닙니다: {filename}: {identifier!r}")
    if "umc-product" not in tags:
        raise ValueError(f"UMC dashboard tag가 없습니다: {filename}")

    if filename in {"postgresql-detail.json", "server-default.json"}:
        serialized = json.dumps(document, ensure_ascii=False)
        serialized_lower = serialized.lower()
        forbidden_dependencies = ("cloudwatch", "aws/rds", "rds_instance", "aws_region")
        present = [value for value in forbidden_dependencies if value in serialized_lower]
        if present:
            raise ValueError(
                f"{filename}에 제거되지 않은 RDS/CloudWatch 의존성이 있습니다: {present}"
            )

    if filename == "postgresql-detail.json":
        serialized = json.dumps(document, ensure_ascii=False)
        required_metrics = (
            "pg_up",
            "pg_stat_activity_count",
            "pg_settings_max_connections",
            "pg_database_size_bytes",
            "pg_stat_database_xact_commit",
            "pg_stat_database_xact_rollback",
            "pg_stat_database_blks_hit",
            "pg_stat_database_deadlocks",
            "pg_locks_count",
            "kube_statefulset_status_replicas_ready",
            "kube_pod_container_resource_requests",
            "kube_pod_container_resource_limits",
            "kube_persistentvolumeclaim_resource_requests_storage_bytes",
        )
        missing_metrics = [metric for metric in required_metrics if metric not in serialized]
        if missing_metrics:
            raise ValueError(
                f"postgresql-detail.json에 필수 PostgreSQL/Kubernetes 패널 지표가 없습니다: {missing_metrics}"
            )

    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def rule_groups(content: str) -> set[str]:
    return {match.group("name") for match in GROUP_NAME.finditer(content)}


def validate_rule_groups(content: str, expected: set[str], filename: str) -> None:
    groups = rule_groups(content)
    missing = expected - groups
    non_umc = sorted(group for group in groups if not group.startswith("umc-product."))
    if missing:
        raise ValueError(f"{filename}에 필수 UMC rule group이 없습니다: {sorted(missing)}")
    if non_umc:
        raise ValueError(f"{filename}에 UMC 외 rule group이 있습니다: {non_umc}")


def drop_alert_rules(content: str, names: frozenset[str]) -> str:
    """Prometheus rule list에서 이름이 일치하는 alert block만 제거한다."""
    lines = content.splitlines(keepends=True)
    output: list[str] = []
    found: set[str] = set()
    index = 0

    while index < len(lines):
        match = ALERT_START.match(lines[index].rstrip("\n"))
        if match is None or match.group("name") not in names:
            output.append(lines[index])
            index += 1
            continue

        found.add(match.group("name"))
        rule_indent = len(match.group("indent"))
        index += 1
        while index < len(lines):
            line = lines[index]
            if line.strip() and len(line) - len(line.lstrip(" ")) <= rule_indent:
                break
            index += 1

    missing = names - found
    if missing:
        raise ValueError(f"k3s에서 제외할 alert가 원본에 없습니다: {sorted(missing)}")
    return "".join(output)


# 현재 K3s에 수집 대상이 없거나 chart 기본 규칙과 겹치는 원본 규칙을 제외한다.
def k3s_prometheus_rules(content: str) -> str:
    validate_rule_groups(
        content,
        {"umc-product.availability", "umc-product.observability"},
        PROMETHEUS_RULE_SOURCE,
    )
    content = drop_alert_rules(content, K3S_DROPPED_ALERTS)
    return (
        "# umc-infra k3s topology: 없는 target과 chart 기본 규칙 중복 제외\n"
        + content.rstrip()
        + "\n"
    )


# 전용 생성 디렉터리에서만 allowlist에 없는 YAML을 지워 오래된 dashboard가 남지 않게 한다.
def remove_stale_dashboard_manifests(directory: pathlib.Path, expected: set[str]) -> None:
    for path in directory.glob("*.yaml"):
        if path.name not in expected:
            path.unlink()
            print(f"removed dashboards/{path.name}")


def desired_outputs(source_root: pathlib.Path) -> dict[pathlib.Path, str]:
    dashboard_source = source_root / "dashboards"
    rule_source = source_root / "rules"
    desired: dict[pathlib.Path, str] = {}
    for filename, folder in DASHBOARD_ALLOWLIST.items():
        source = dashboard_source / filename
        dashboard = canonical_dashboard(filename, read_text(source))
        relative_output = DASHBOARD_DIRECTORY / pathlib.Path(filename).with_suffix(".yaml")
        desired[relative_output] = configmap(
            f"dashboard-{pathlib.Path(filename).stem.replace('_', '-')}",
            filename,
            dashboard,
            folder=folder,
        )

    prometheus = k3s_prometheus_rules(read_text(rule_source / PROMETHEUS_RULE_SOURCE))
    desired[pathlib.Path("observability-integrations/prometheus-alert-rules.yaml")] = (
        prometheus_rule(prometheus)
    )

    loki = read_text(rule_source / LOKI_RULE_SOURCE)
    validate_rule_groups(loki, {"umc-product.logs"}, LOKI_RULE_SOURCE)
    desired[pathlib.Path("observability/loki-alert-rules.yaml")] = configmap(
        "loki-alert-rules", LOKI_RULE_KEY, loki.rstrip() + "\n"
    )
    return desired


def parse_arguments() -> tuple[pathlib.Path, bool]:
    arguments = list(sys.argv[1:])
    check = False
    if "--check" in arguments:
        arguments.remove("--check")
        check = True
    if len(arguments) > 1 or any(argument.startswith("-") for argument in arguments):
        raise ValueError(
            "사용법: python3 scripts/gen-configmaps.py [--check] [observability_경로]"
        )
    source = pathlib.Path(arguments[0]).resolve() if arguments else ROOT / "observability"
    return source, check


# 모든 원본을 검증한 뒤 기록한다. --check는 파일을 고치지 않고 누락·변경·잔여 파일만 보고한다.
def main() -> int:
    source, check = parse_arguments()
    desired = desired_outputs(source)
    expected_dashboards = {
        path.name for path in desired if path.parent == DASHBOARD_DIRECTORY
    }
    dashboard_output = OUTPUT / DASHBOARD_DIRECTORY

    if check:
        drift: list[str] = []
        if (OUTPUT / "observability/prometheus-alert-rules.yaml").exists():
            drift.append("stale: observability/prometheus-alert-rules.yaml (legacy ConfigMap)")
        actual_dashboards = {
            path.name for path in dashboard_output.glob("*.yaml") if path.is_file()
        }
        for stale in sorted(actual_dashboards - expected_dashboards):
            drift.append(f"stale: dashboards/{stale}")
        for relative_path, content in desired.items():
            output = OUTPUT / relative_path
            if not output.is_file():
                drift.append(f"missing: {relative_path}")
            elif output.read_text(encoding="utf-8") != content:
                drift.append(f"changed: {relative_path}")
        if drift:
            print("관측성 생성물이 원본과 다릅니다:", file=sys.stderr)
            for item in drift:
                print(f"  - {item}", file=sys.stderr)
            print("python3 scripts/gen-configmaps.py 를 실행하세요.", file=sys.stderr)
            return 1
        print(f"관측성 생성물 {len(desired)}개가 원본과 일치합니다.")
        return 0

    dashboard_output.mkdir(parents=True, exist_ok=True)
    remove_stale_dashboard_manifests(dashboard_output, expected_dashboards)
    for relative_path, content in desired.items():
        output = OUTPUT / relative_path
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content, encoding="utf-8")
        print(relative_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

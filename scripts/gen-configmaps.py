#!/usr/bin/env python3
"""관측성 원본을 Kubernetes ConfigMap으로 결정적으로 변환한다.

원본은 이 저장소의 ``observability/``가 소유한다. 배포가 검증된 초기 허용
목록만 ``manifests/observability/``에 생성하며, 허용 목록에서 빠진 원본은
보관하더라도 Grafana sidecar에 전달하지 않는다.

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
OUTPUT = ROOT / "manifests" / "observability"

DASHBOARD_ALLOWLIST = (
    "api-flow.json",
    "cache.json",
    "graphql.json",
    "node-exporter-host.json",
    "server-default.json",
    "server-logs.json",
)
PROMETHEUS_RULE_SOURCE = "prometheus-alerts.yaml"
LOKI_RULE_SOURCE = "loki-alerts.yaml"
PROMETHEUS_RULE_KEY = "default-alerts.yml"
LOKI_RULE_KEY = "default-log-alerts.yml"

ALERT_START = re.compile(r"^(?P<indent> *)- alert: (?P<name>[^ ]+)\s*$")
GROUP_NAME = re.compile(r"^  - name: (?P<name>\S+)\s*$", re.MULTILINE)
AVAILABILITY_MARKER = "  - name: umc-product.availability\n    rules:\n"
OBSERVABILITY_MARKER = "  - name: umc-product.observability\n    rules:\n"

# 현재 k3s scrape topology에 target이 없거나 아래 전용 규칙과 중복된다.
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
    }
)
COMMENTED_NODE_EXPORTER_ALERT = """\
      # - alert: NodeExporterDown
      #   expr: up{job="node-exporter"} == 0
      #   for: 3m
      #   labels:
      #     severity: warning
      #   annotations:
      #     summary: "Node exporter is down"
      #     description: "Host metrics are not being scraped from {{ $labels.instance }}."

"""
K3S_STATIC_ALERTS = """\
      # k3s에서 실제로 scrape하는 Service와 port를 기준으로 한다.
      - alert: NodeExporterDown
        expr: up{job="node-exporter"} == 0
        for: 3m
        labels:
          severity: warning
        annotations:
          summary: "Node exporter scrape를 확인해주세요"
          description: "instance={{ $labels.instance }} 의 exporter 또는 cluster 내부 scrape 경로가 3분 이상 응답하지 않아요. 전체 node/cluster 장애 탐지는 외부 모니터가 별도로 필요해요."

      - alert: TempoDown
        expr: up{job="tempo"} == 0
        for: 3m
        labels:
          severity: warning
        annotations:
          summary: "Tempo metrics endpoint를 확인해주세요"
          description: "instance={{ $labels.instance }} 의 Tempo metrics endpoint를 3분 이상 scrape하지 못했어요. Tempo pod, Service와 storage 상태를 확인해주세요."

      - alert: KubeStateMetricsDown
        expr: up{job="kube-state-metrics"} == 0
        for: 3m
        labels:
          severity: warning
        annotations:
          summary: "kube-state-metrics scrape를 확인해주세요"
          description: "instance={{ $labels.instance }} 에서 backup Job/CronJob 상태 지표를 3분 이상 받지 못했어요. kube-state-metrics pod와 Service를 확인해주세요."

"""
K3S_OBSERVABILITY_ALERTS = """\
      # 100% sampling 정책과 실제 전달 성공은 다르다. Collector가 span을 거부하거나
      # exporter queue가 포화되면 유실될 수 있으므로 별도 경보로 드러낸다.
      - alert: OTelCollectorTraceReceiveRefusals
        expr: sum by (receiver, transport, instance) (rate(otelcol_receiver_refused_spans[5m]) or rate(otelcol_receiver_refused_spans_total[5m])) > 0
        for: 3m
        labels:
          severity: critical
        annotations:
          summary: "OTel Collector가 trace 수신을 거부하고 있어요"
          description: "receiver={{ $labels.receiver }}, instance={{ $labels.instance }} 에서 span이 3분 이상 거부되고 있어요. memory limiter, Collector RSS와 client retry를 확인해주세요."

      - alert: OTelCollectorTraceEnqueueFailures
        expr: sum by (exporter, instance) (rate(otelcol_exporter_enqueue_failed_spans[5m]) or rate(otelcol_exporter_enqueue_failed_spans_total[5m])) > 0
        for: 3m
        labels:
          severity: critical
        annotations:
          summary: "OTel Collector trace queue 유실을 확인해주세요"
          description: "exporter={{ $labels.exporter }}, instance={{ $labels.instance }} 의 queue에 span을 넣지 못하고 있어요. Tempo 처리량과 exporter queue를 확인해주세요."

      - alert: OTelCollectorExporterQueueHigh
        expr: max by (exporter, data_type, instance) (otelcol_exporter_queue_size{data_type="traces"} / clamp_min(otelcol_exporter_queue_capacity{data_type="traces"}, 1)) > 0.7
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "OTel Collector exporter queue가 70%를 넘었어요"
          description: "exporter={{ $labels.exporter }}, data_type={{ $labels.data_type }}, instance={{ $labels.instance }} 의 queue가 5분 이상 70%를 넘었어요. downstream 지연과 trace 유입량을 확인해주세요."

      - alert: PostgresBackupFailed
        expr: max by (namespace, job_name) (kube_job_failed{namespace="db",job_name=~"pg-backup-.+",condition="true"}) > 0
        for: 0m
        labels:
          severity: critical
        annotations:
          summary: "PostgreSQL S3 backup Job이 실패했어요"
          description: "job={{ $labels.job_name }} 의 dump, checksum, S3 권한과 bucket 상태를 확인해주세요."

      - alert: PostgresBackupNeverSucceeded
        expr: kube_cronjob_spec_suspend{namespace="db",cronjob="pg-backup"} == 0 unless on (namespace, cronjob) kube_cronjob_status_last_successful_time{namespace="db",cronjob="pg-backup"}
        for: 27h
        labels:
          severity: critical
        annotations:
          summary: "활성화된 PostgreSQL S3 backup의 정규 성공 기록이 없어요"
          description: "pg-backup을 활성화한 뒤 27시간 동안 CronJob status에 성공 기록이 없어요. 첫 schedule, controller, Job과 S3 writer 권한을 확인해주세요. 수동 create job은 이 status를 채우지 않아요."

      - alert: PostgresBackupStale
        expr: kube_cronjob_spec_suspend{namespace="db",cronjob="pg-backup"} == 0 and on (namespace, cronjob) time() - kube_cronjob_status_last_successful_time{namespace="db",cronjob="pg-backup"} > 108000
        for: 10m
        labels:
          severity: critical
        annotations:
          summary: "최근 PostgreSQL S3 backup 성공 기록이 없어요"
          description: "활성화된 pg-backup의 마지막 정규 성공이 30시간을 넘었어요. CronJob, ExternalSecret과 S3 writer 권한을 확인해주세요."

"""
PROMETHEUS_COUNTER_REPLACEMENTS = {
    "rate(otelcol_exporter_send_failed_log_records[5m])": (
        "(rate(otelcol_exporter_send_failed_log_records[5m]) or "
        "rate(otelcol_exporter_send_failed_log_records_total[5m]))"
    ),
    "rate(otelcol_exporter_send_failed_metric_points[5m])": (
        "(rate(otelcol_exporter_send_failed_metric_points[5m]) or "
        "rate(otelcol_exporter_send_failed_metric_points_total[5m]))"
    ),
    "rate(otelcol_exporter_send_failed_spans[5m])": (
        "(rate(otelcol_exporter_send_failed_spans[5m]) or "
        "rate(otelcol_exporter_send_failed_spans_total[5m]))"
    ),
}


def read_text(path: pathlib.Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"필수 관측성 원본이 없습니다: {path}")
    return path.read_text(encoding="utf-8")


def block_scalar(text: str, indent: int) -> str:
    pad = " " * indent
    return "\n".join((pad + line).rstrip() for line in text.splitlines())


def configmap(name: str, filename: str, content: str, *, dashboard: bool = False) -> str:
    dashboard_label = '    grafana_dashboard: "1"\n' if dashboard else ""
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
        "data:\n"
        f"  {filename}: |\n"
        f"{block_scalar(content, 4)}\n"
    )


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

    if filename == "server-default.json":
        serialized = json.dumps(document, ensure_ascii=False)
        serialized_lower = serialized.lower()
        forbidden_dependencies = ("cloudwatch", "aws/rds", "rds_instance", "aws_region")
        present = [value for value in forbidden_dependencies if value in serialized_lower]
        if present:
            raise ValueError(
                f"server-default.json에 제거되지 않은 RDS/CloudWatch 의존성이 있습니다: {present}"
            )

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
                f"server-default.json에 필수 PostgreSQL/Kubernetes 패널 지표가 없습니다: {missing_metrics}"
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


def k3s_prometheus_rules(content: str) -> str:
    validate_rule_groups(
        content,
        {"umc-product.availability", "umc-product.observability"},
        PROMETHEUS_RULE_SOURCE,
    )
    content = drop_alert_rules(content, K3S_DROPPED_ALERTS)
    if content.count(COMMENTED_NODE_EXPORTER_ALERT) != 1:
        raise ValueError("교체할 NodeExporterDown 주석을 정확히 하나 찾지 못했습니다")
    content = content.replace(COMMENTED_NODE_EXPORTER_ALERT, "", 1)

    for marker, addition in (
        (AVAILABILITY_MARKER, K3S_STATIC_ALERTS),
        (OBSERVABILITY_MARKER, K3S_OBSERVABILITY_ALERTS),
    ):
        if content.count(marker) != 1:
            raise ValueError(f"삽입할 UMC rule group marker를 정확히 하나 찾지 못했습니다: {marker!r}")
        content = content.replace(marker, marker + addition, 1)

    for old, new in PROMETHEUS_COUNTER_REPLACEMENTS.items():
        if content.count(old) != 1:
            raise ValueError(f"교체할 OTel counter expression을 정확히 하나 찾지 못했습니다: {old}")
        content = content.replace(old, new, 1)

    return (
        "# umc-infra k3s topology: 없는 target 규칙 제외, 내부 target·전달 무결성 규칙 추가\n"
        + content.rstrip()
        + "\n"
    )


def remove_stale_dashboard_manifests(directory: pathlib.Path, expected: set[str]) -> None:
    for path in directory.glob("*.yaml"):
        if path.name not in expected:
            path.unlink()
            print(f"removed dashboards/{path.name}")


def desired_outputs(source_root: pathlib.Path) -> dict[pathlib.Path, str]:
    dashboard_source = source_root / "dashboards"
    rule_source = source_root / "rules"
    desired: dict[pathlib.Path, str] = {}
    for filename in DASHBOARD_ALLOWLIST:
        source = dashboard_source / filename
        dashboard = canonical_dashboard(filename, read_text(source))
        relative_output = pathlib.Path("dashboards") / pathlib.Path(filename).with_suffix(".yaml")
        desired[relative_output] = configmap(
            f"dashboard-{pathlib.Path(filename).stem.replace('_', '-')}",
            filename,
            dashboard,
            dashboard=True,
        )

    prometheus = k3s_prometheus_rules(read_text(rule_source / PROMETHEUS_RULE_SOURCE))
    desired[pathlib.Path("prometheus-alert-rules.yaml")] = configmap(
        "prometheus-alert-rules", PROMETHEUS_RULE_KEY, prometheus
    )

    loki = read_text(rule_source / LOKI_RULE_SOURCE)
    validate_rule_groups(loki, {"umc-product.logs"}, LOKI_RULE_SOURCE)
    desired[pathlib.Path("loki-alert-rules.yaml")] = configmap(
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


def main() -> int:
    source, check = parse_arguments()
    desired = desired_outputs(source)
    expected_dashboards = {
        path.name for path in desired if path.parent == pathlib.Path("dashboards")
    }
    dashboard_output = OUTPUT / "dashboards"

    if check:
        drift: list[str] = []
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

#!/usr/bin/env python3
"""배포하는 dashboard 원본의 Prometheus 쿼리가 운영에서 그려지는지 검사한다.

두 가지를 본다.

1. 메트릭 존재. 패널 쿼리가 참조하는 메트릭 이름이 ``observability/prometheus-metric-names.txt``에
   있어야 한다. 그 파일은 운영 Prometheus에서 확인한 이름의 기록이다. 운영에 없는 메트릭을 그리는
   패널은 항상 비어 있으므로 PR 단계에서 막는다.
2. PromQL 문법. ``--promql``이면 dashboard 변수를 자리값으로 바꾼 뒤 ``promtool check rules``로
   파싱한다. 잘못된 문자열 이스케이프처럼 Grafana를 열어야만 드러나는 오류를 잡는다.

    python3 scripts/validate_dashboards.py
    python3 scripts/validate_dashboards.py --promql
    python3 scripts/validate_dashboards.py --list-metric-names
    python3 scripts/validate_dashboards.py --refresh-metric-names http://127.0.0.1:9090

``{__name__=~"..."}`` 형태는 이름을 뽑지 못하므로 검사하지 않는다.
갱신은 port-forward 등으로 운영 Prometheus에 닿을 때만 된다. 참조하는 이름이 운영에 없으면 파일을
쓰지 않고 실패한다. 첫 사건 뒤에야 생기는 counter처럼 없어도 되는 이름은 파일에 ``이름  # 이유``로
직접 남긴다. 갱신은 그 줄을 보존한다.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = ROOT / "observability" / "dashboards"
METRIC_NAMES_FILE = ROOT / "observability" / "prometheus-metric-names.txt"
TRAEFIK_POD_MONITOR_FILE = ROOT / "manifests" / "observability-integrations" / "pod-monitors.yaml"
METRIC_NAMES_HEADER = (
    "# 운영 Prometheus /api/v1/label/__name__/values 에서 확인한, dashboard 패널이 참조하는 메트릭 이름.\n"
    "# 갱신: python3 scripts/validate_dashboards.py --refresh-metric-names <prometheus-url>\n"
    "# 이름 뒤에 '# 이유'가 있으면 운영에 아직 없어도 허용한다. 첫 사건 뒤에 생기는 counter가 그렇다.\n"
)

# Grafana가 쿼리 실행 직전에 채우는 내장 변수. 파싱에는 문법만 맞는 값이면 된다.
BUILTIN_VARIABLES = {
    "__rate_interval": "5m",
    "__interval": "5m",
    "__range": "5m",
    "__auto": "5m",
    "__interval_ms": "300000",
    "__range_ms": "300000",
    "__range_s": "300",
    "__from": "0",
    "__to": "0",
}
# label_replace의 "$1" 같은 치환 그룹은 변수가 아니다. 변수 이름은 글자나 밑줄로 시작한다.
VARIABLE_REF = re.compile(r"\$\{([A-Za-z_]\w*)(?::\w+)?\}|\$([A-Za-z_]\w*)")
STRING_LITERAL = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|`[^`]*`')
LABEL_LIST = re.compile(r"\b(by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)")
BRACES = re.compile(r"\{[^}]*\}")
BRACKETS = re.compile(r"\[[^\]]*\]")
IDENTIFIER = re.compile(r"(?<![\w:])([A-Za-z_:][\w:]*)")
PROMQL_KEYWORDS = {
    "and", "or", "unless", "by", "without", "on", "ignoring", "group_left", "group_right",
    "bool", "offset", "atan2", "inf", "nan", "Inf", "NaN",
}


@dataclass(frozen=True)
class PanelQuery:
    dashboard: str
    panel: str
    ref: str
    expr: str

    @property
    def location(self) -> str:
        return f"{self.dashboard} {self.panel} {self.ref}"


def deployed_dashboards() -> list[Path]:
    """gen-configmaps.py의 허용 목록만 검사한다. 보관만 하는 원본은 Grafana에 가지 않는다."""
    spec = importlib.util.spec_from_file_location("gen_configmaps", ROOT / "scripts" / "gen-configmaps.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return [DASHBOARD_DIR / name for name in module.DASHBOARD_ALLOWLIST]


def placeholder(kind: str, current: object, options: list) -> str:
    """변수 종류에 맞는 자리값.

    interval은 기간이어야 range selector 안에서 파싱된다. 나머지는 문자열 안에서도 숫자 자리에서도
    파싱되는 "1"을 쓴다. 글자 자리값은 문자열 밖에서 메트릭 이름으로 잘못 잡힌다.
    """
    if kind in {"IntervalVariable", "interval"}:
        return "5m"
    if kind in {"CustomVariable", "custom"}:
        values = current if isinstance(current, list) else [current]
        for value in values:
            if isinstance(value, str) and value and not value.startswith("$__"):
                return value
        for option in options:
            value = option.get("value") if isinstance(option, dict) else option
            if isinstance(value, str) and value and not value.startswith("$__"):
                return value
    return "1"


def dashboard_variables(dashboard: dict) -> dict[str, str]:
    variables = dict(BUILTIN_VARIABLES)
    if dashboard.get("apiVersion", "").startswith("dashboard.grafana.app/v2"):
        for variable in dashboard["spec"].get("variables", []):
            spec = variable["spec"]
            current = spec.get("current", {})
            current = current.get("value") if isinstance(current, dict) else current
            variables[spec["name"]] = placeholder(variable["kind"], current, spec.get("options", []))
        return variables
    for variable in dashboard.get("templating", {}).get("list", []):
        current = variable.get("current", {})
        current = current.get("value") if isinstance(current, dict) else current
        variables[variable["name"]] = placeholder(variable.get("type", ""), current, variable.get("options", []))
    return variables


def substitute_variables(expr: str, variables: dict[str, str]) -> str:
    def replace(match: re.Match) -> str:
        name = match.group(1) or match.group(2)
        if name not in variables:
            raise ValueError(f"정의되지 않은 dashboard 변수 ${name}")
        return variables[name]

    return VARIABLE_REF.sub(replace, expr)


def panel_queries(path: Path) -> list[PanelQuery]:
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    variables = dashboard_variables(dashboard)
    queries = []
    for panel, ref, expr in raw_queries(dashboard):
        try:
            queries.append(PanelQuery(path.name, panel, ref, substitute_variables(expr, variables)))
        except ValueError as error:
            raise SystemExit(f"{path.name} {panel} {ref}: {error}") from error
    return queries


def raw_queries(dashboard: dict):
    if dashboard.get("apiVersion", "").startswith("dashboard.grafana.app/v2"):
        for name, element in dashboard["spec"]["elements"].items():
            if element.get("kind") != "Panel":
                continue
            for query in element["spec"].get("data", {}).get("spec", {}).get("queries", []):
                data_query = query["spec"]["query"]
                expr = data_query.get("spec", {}).get("expr")
                if data_query.get("group") == "prometheus" and expr:
                    yield name, query["spec"].get("refId", "?"), expr
        return
    for panel in v1_panels(dashboard.get("panels", [])):
        for target in panel.get("targets", []):
            datasource = target.get("datasource") or panel.get("datasource") or {}
            expr = target.get("expr")
            if isinstance(datasource, dict) and datasource.get("type") == "prometheus" and expr:
                yield f"panel-{panel.get('id', '?')}", target.get("refId", "?"), expr


def v1_panels(panels: list):
    for panel in panels:
        yield panel
        yield from v1_panels(panel.get("panels", []))


def metric_names(expr: str) -> set[str]:
    """PromQL에서 메트릭 이름만 고른다.

    label 이름은 by/on 같은 괄호 목록과 중괄호 안에만 있으므로 먼저 지운다. 그러면 함수와
    집계 연산자는 모두 '('가 바로 따르고, 남는 식별자가 메트릭 이름이다.
    """
    text = STRING_LITERAL.sub('""', expr)
    text = LABEL_LIST.sub(" ", text)
    text = BRACES.sub("{}", text)
    text = BRACKETS.sub("[]", text)
    names = set()
    for match in IDENTIFIER.finditer(text):
        name = match.group(1)
        rest = text[match.end():].lstrip()
        if name in PROMQL_KEYWORDS or rest.startswith("("):
            continue
        names.add(name)
    return names


def read_metric_names(path: Path = METRIC_NAMES_FILE) -> dict[str, str | None]:
    """이름 → 허용 이유. 이유가 None이면 운영에서 확인된 이름이다."""
    known: dict[str, str | None] = {}
    if not path.exists():
        return known
    for line in path.read_text(encoding="utf-8").splitlines():
        body, _, comment = line.partition("#")
        name = body.strip()
        if name:
            known[name] = comment.strip() or None
    return known


def check_metric_names(queries: list[PanelQuery], known: dict[str, str | None]) -> list[str]:
    problems = []
    for query in queries:
        for name in sorted(metric_names(query.expr)):
            if name not in known:
                problems.append(f"{query.location}: {name}")
    return problems


def read_traefik_pod_monitor(path: Path = TRAEFIK_POD_MONITOR_FILE) -> dict:
    return next(resource for resource in yaml.safe_load_all(path.read_text(encoding="utf-8"))
                if resource and resource["kind"] == "PodMonitor" and resource["metadata"]["name"] == "traefik")


# Traefik PodMonitor는 keep regex 밖의 traefik_ 메트릭을 버린다. 그 이름을 그리는 패널은 항상 비어 있다.
def check_traefik_keep(queries: list[PanelQuery], monitor: dict) -> list[str]:
    regexes = [relabel["regex"] for endpoint in monitor["spec"]["podMetricsEndpoints"]
               for relabel in endpoint.get("metricRelabelings", [])
               if relabel.get("action") == "keep" and relabel.get("sourceLabels") == ["__name__"]]
    return [f"{query.location}: {name}"
            for query in queries for name in sorted(metric_names(query.expr))
            if name.startswith("traefik_") and not all(re.fullmatch(regex, name) for regex in regexes)]


def record_name(query: PanelQuery) -> str:
    return "dashboard_check:" + re.sub(r"[^A-Za-z0-9_:]", "_", f"{query.dashboard}:{query.panel}_{query.ref}")


def check_promql(queries: list[PanelQuery], promtool: str) -> list[str]:
    """dashboard마다 쿼리를 recording rule로 묶어 promtool에 넘긴다. rule 이름이 패널을 가리킨다."""
    problems = []
    by_dashboard: dict[str, list[PanelQuery]] = {}
    for query in queries:
        by_dashboard.setdefault(query.dashboard, []).append(query)
    with tempfile.TemporaryDirectory() as directory:
        for dashboard, dashboard_queries in by_dashboard.items():
            rules = {"groups": [{"name": dashboard, "rules": [
                {"record": record_name(query), "expr": query.expr} for query in dashboard_queries
            ]}]}
            path = Path(directory, dashboard + ".rules.json")
            path.write_text(json.dumps(rules), encoding="utf-8")
            result = subprocess.run([promtool, "check", "rules", str(path)], capture_output=True, text=True, check=False)
            if result.returncode != 0:
                problems.append(f"{dashboard}:\n{result.stdout}{result.stderr}")
    return problems


def refresh_metric_names(url: str, queries: list[PanelQuery], path: Path = METRIC_NAMES_FILE) -> list[str]:
    with urllib.request.urlopen(url.rstrip("/") + "/api/v1/label/__name__/values", timeout=30) as response:
        live = set(json.load(response)["data"])
    existing = read_metric_names(path)
    referenced = sorted({name for query in queries for name in metric_names(query.expr)})
    missing = [name for name in referenced if name not in live and not existing.get(name)]
    if missing:
        return missing
    # 이유는 운영에 지금 있어도 보존한다. 첫 실패 뒤 생긴 counter는 보관 기간이 지나면 다시 사라진다.
    lines = [METRIC_NAMES_HEADER]
    for name in referenced:
        lines.append(f"{name}  # {existing[name]}" if existing.get(name) else name)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--promql", action="store_true", help="promtool로 PromQL 문법을 검사한다")
    parser.add_argument("--list-metric-names", action="store_true", help="패널이 참조하는 메트릭 이름을 출력한다")
    parser.add_argument("--refresh-metric-names", metavar="PROMETHEUS_URL",
                        help="운영 Prometheus에서 이름을 받아 기록 파일을 다시 쓴다")
    args = parser.parse_args(argv)

    queries = [query for path in deployed_dashboards() for query in panel_queries(path)]

    if args.list_metric_names:
        for name in sorted({name for query in queries for name in metric_names(query.expr)}):
            print(name)
        return 0

    if args.refresh_metric_names:
        missing = refresh_metric_names(args.refresh_metric_names, queries)
        if missing:
            print("운영 Prometheus에 없는 메트릭을 참조합니다. 패널을 고치거나 기록 파일에 이유를 적으세요.", file=sys.stderr)
            for name in missing:
                print(f"  - {name}", file=sys.stderr)
            return 1
        print(f"{METRIC_NAMES_FILE.relative_to(ROOT)} 갱신")
        return 0

    if args.promql:
        promtool = shutil.which("promtool")
        if promtool is None:
            print("promtool is required for --promql", file=sys.stderr)
            return 1
        problems = check_promql(queries, promtool)
        label = "PromQL 파싱 실패"
    else:
        problems = check_metric_names(queries, read_metric_names())
        label = f"{METRIC_NAMES_FILE.relative_to(ROOT)}에 없는 메트릭을 참조"
        if not problems:
            problems = check_traefik_keep(queries, read_traefik_pod_monitor())
            label = f"{TRAEFIK_POD_MONITOR_FILE.relative_to(ROOT)}의 keep regex가 버리는 메트릭을 참조"

    if problems:
        print(f"dashboard validation failed: {label}", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"dashboard validation passed ({len(queries)} Prometheus queries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

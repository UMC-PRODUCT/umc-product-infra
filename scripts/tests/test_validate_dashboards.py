#!/usr/bin/env python3
"""dashboard 패널 쿼리 검사가 운영에 없는 메트릭과 깨진 PromQL을 잡는지 확인한다."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from validate_dashboards import (
    BUILTIN_VARIABLES,
    PanelQuery,
    check_metric_names,
    check_promql,
    check_traefik_keep,
    dashboard_variables,
    deployed_dashboards,
    metric_names,
    panel_queries,
    raw_queries,
    read_metric_names,
    read_traefik_pod_monitor,
    refresh_metric_names,
    substitute_variables,
)


class MetricNamesTest(unittest.TestCase):
    def test_함수와_label_이름은_메트릭이_아니다(self) -> None:
        expr = ('histogram_quantile(0.95, sum by (le, uri) '
                '(rate(http_server_requests_milliseconds_bucket{application=~"x", uri!="UNKNOWN"}[5m]))) > 0')
        self.assertEqual(metric_names(expr), {"http_server_requests_milliseconds_bucket"})

    def test_연산자_양쪽_메트릭을_모두_찾고_문자열은_무시한다(self) -> None:
        expr = ('label_replace(a_total, "k", "$1", "v", "(.*)") / on(instance) group_left(job) b_total '
                'or vector(0) unless absent(c_total) and d_total offset 5m > bool 1 or e_total atan2 f_total')
        self.assertEqual(metric_names(expr), {"a_total", "b_total", "c_total", "d_total", "e_total", "f_total"})


class VariablesTest(unittest.TestCase):
    def test_정의되지_않은_변수는_실패한다(self) -> None:
        with self.assertRaises(ValueError):
            substitute_variables('up{job="$job"}', BUILTIN_VARIABLES)

    def test_interval과_custom_변수는_실제_값_형태로_바뀐다(self) -> None:
        dashboard = {"apiVersion": "dashboard.grafana.app/v2beta1", "spec": {"variables": [
            {"kind": "IntervalVariable", "spec": {"name": "step", "current": {"value": "2m"}}},
            {"kind": "CustomVariable", "spec": {"name": "os", "current": {"value": "linux"}, "options": []}},
            {"kind": "CustomVariable", "spec": {"name": "svc", "current": {"value": "$__all"},
                                                "options": [{"text": "All", "value": "$__all"}, {"text": "a", "value": "a"}]}},
            {"kind": "QueryVariable", "spec": {"name": "instance", "current": {"value": ["$__all"]}}},
        ]}}
        variables = dashboard_variables(dashboard)
        expr = 'rate(x{os="$os", svc=~"${svc:regex}", instance=~"$instance"}[$step]) / rate(y[$__rate_interval])'
        self.assertEqual(substitute_variables(expr, variables),
                         'rate(x{os="linux", svc=~"a", instance=~"1"}[5m]) / rate(y[5m])')

    def test_label_replace의_치환_그룹은_변수가_아니다(self) -> None:
        expr = 'label_replace(up, "k", "$1", "instance", "(.*)") > $__range_s'
        self.assertEqual(substitute_variables(expr, BUILTIN_VARIABLES),
                         'label_replace(up, "k", "$1", "instance", "(.*)") > 300')

    def test_문자열_밖의_변수_자리값은_메트릭으로_잡히지_않는다(self) -> None:
        dashboard = {"apiVersion": "dashboard.grafana.app/v2beta1", "spec": {"variables": [
            {"kind": "TextVariable", "spec": {"name": "threshold", "current": {"value": "0.5"}}},
        ]}}
        expr = substitute_variables("up > $threshold", dashboard_variables(dashboard))
        self.assertEqual(metric_names(expr), {"up"})

    def test_배포_대시보드의_변수는_모두_정의되어_있다(self) -> None:
        # Loki·Tempo만 쓰는 dashboard는 Prometheus 쿼리가 없어도 된다. 정의되지 않은 변수만 실패다.
        queries = [query for path in deployed_dashboards() for query in panel_queries(path)]
        self.assertTrue(queries)


class MetricNamesFileTest(unittest.TestCase):
    def test_이유가_적힌_이름은_운영에_없어도_허용한다(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "names.txt")
            path.write_text("# 주석\nlive_total\nlazy_total  # 첫 사건 뒤 생성\n", encoding="utf-8")
            known = read_metric_names(path)
        self.assertEqual(known, {"live_total": None, "lazy_total": "첫 사건 뒤 생성"})
        queries = [PanelQuery("d.json", "panel-1", "A", "live_total + lazy_total + gone_total")]
        self.assertEqual(check_metric_names(queries, known), ["d.json panel-1 A: gone_total"])

    def test_갱신은_운영에_있는_이름의_이유도_보존한다(self) -> None:
        queries = [PanelQuery("d.json", "panel-1", "A", "live_total + lazy_total")]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "names.txt")
            path.write_text("lazy_total  # 첫 사건 뒤 생성\n", encoding="utf-8")
            with mock.patch("validate_dashboards.urllib.request.urlopen") as urlopen:
                urlopen.return_value.__enter__.return_value = io.StringIO(
                    json.dumps({"data": ["live_total", "lazy_total"]}))
                self.assertEqual(refresh_metric_names("http://prom", queries, path), [])
            self.assertEqual(read_metric_names(path), {"live_total": None, "lazy_total": "첫 사건 뒤 생성"})


class TraefikKeepTest(unittest.TestCase):
    def test_PodMonitor가_버리는_traefik_메트릭을_그리면_실패한다(self) -> None:
        monitor = read_traefik_pod_monitor()
        queries = [PanelQuery("d.json", "panel-1", "A", "traefik_entrypoint_requests_total - traefik_service_requests_total"),
                   PanelQuery("d.json", "panel-2", "A", "traefik_router_requests_total + up")]
        self.assertEqual(check_traefik_keep(queries, monitor), ["d.json panel-2 A: traefik_router_requests_total"])


class PromqlCheckTest(unittest.TestCase):
    def test_잘못된_문자열_이스케이프를_잡는다(self) -> None:
        promtool = shutil.which("promtool")
        if promtool is None:
            self.skipTest("promtool not installed; dashboard PromQL parse test skipped")
        queries = [
            PanelQuery("a.json", "panel-1", "A", 'up{fstype!~"fuse\\..*"}'),
            PanelQuery("b.json", "panel-2", "A", 'up{fstype!~"fuse\\\\..*"}'),
        ]
        problems = check_promql(queries, promtool)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("a.json", problems[0])
        self.assertIn("panel_1_A", problems[0])
        self.assertIn("unknown escape sequence", problems[0])


ENTRYPOINT_429 = 'traefik_entrypoint_requests_total{code="429", entrypoint="websecure", method="GET", protocol="http"}'
SERVICE_429 = 'traefik_service_requests_total{code="429", service="app-umc-product-server-http@kubernetes", method="GET", protocol="http"}'
APP_BLOCKED = 'api_rate_limit_requests_total{application="prod-umc-product", instance="a", result="blocked", rule="login"}'
APP_ALLOWED = 'api_rate_limit_requests_total{application="prod-umc-product", instance="a", result="allowed", rule="login"}'
APP_RULE = '{application="prod-umc-product", rule="login"}'
APP_NO_BLOCK = '{application="prod-umc-product", rule="증가 없음"}'
APP_TOTAL = '{application="prod-umc-product"}'
DEV_ALLOWED = APP_ALLOWED.replace("prod-umc-product", "dev-umc-product")
DEV_NO_BLOCK = APP_NO_BLOCK.replace("prod-umc-product", "dev-umc-product")
HTTP_200 = 'http_server_requests_milliseconds_count{application="prod-umc-product", instance="a", method="GET", status="200", uri="/api/a"}'
HTTP_429 = HTTP_200.replace('status="200"', 'status="429"')
DEV_HTTP_200 = HTTP_200.replace("prod-umc-product", "dev-umc-product")
DEV_HTTP_429 = HTTP_429.replace("prod-umc-product", "dev-umc-product")
API_ROW = '{application="prod-umc-product", method="GET", uri="/api/a"}'
DEV_API_ROW = API_ROW.replace("prod-umc-product", "dev-umc-product")
API_PLACEHOLDER = '{application="prod-umc-product", method="-", uri="429 증가 없음"}'
DEV_API_PLACEHOLDER = API_PLACEHOLDER.replace("prod-umc-product", "dev-umc-product")
PANEL_VARIABLES = {"__rate_interval": "2m", "__range": "10m", "service": ".+", "instance": ".+"}
RATE_LIMIT_PANELS = {
    "server-default.json": {("panel-34", "A"): "app_rate", ("panel-35", "A"): "app_total", ("panel-7", "A"): "api_table"},
    "system-overview.json": {("panel-16", "A"): "edge_rate", ("panel-17", "A"): "edge_total"},
}
# 30초 scrape를 흉내 낸다. series 값 '0+600x20'은 30초마다 600씩, 10분 동안 늘어나는 counter다.
# 기대값의 [] 는 No data, {} 는 label 없는 합계 한 줄이다. 외삽 오차 때문에 소수 셋째 자리로 반올림해 비교한다.
RATE_LIMIT_SCENARIOS: dict[str, tuple[str, dict[str, str], dict[str, list[tuple[str, float]]]]] = {
    "edge는_Traefik_429에서_앱_429를_뺀_값이고_앱은_blocked만_센다": ("10m", {
        ENTRYPOINT_429: "0+600x20", SERVICE_429: "0+240x20", APP_BLOCKED: "0+240x20", APP_ALLOWED: "0+3000x20",
    }, {"edge_rate": [("{}", 12)], "edge_total": [("{}", 7200)],
        "app_rate": [(APP_RULE, 8)], "app_total": [(APP_TOTAL, 4800)]}),
    "앱_429가_한_번도_없으면_Traefik_429_전부가_edge다": ("10m", {
        ENTRYPOINT_429: "0+600x20",
        SERVICE_429.replace('code="429"', 'code="200"'): "0+1000x20",
    }, {"edge_rate": [("{}", 20)], "edge_total": [("{}", 12000)]}),
    # series마다 외삽이 달라 service 429가 entrypoint 429보다 크게 계산되는 구간이 있다.
    "뺀_값이_음수면_0이다": ("10m", {
        ENTRYPOINT_429: "0+60x20", SERVICE_429: "0+240x20",
    }, {"edge_rate": [("{}", 0)], "edge_total": [("{}", 0)]}),
    "수집되는_중_차단이_없으면_edge와_앱_건수는_0이다": ("10m", {
        ENTRYPOINT_429.replace('code="429"', 'code="200"'): "0+1000x20", APP_ALLOWED: "0+3000x20",
    }, {"edge_rate": [("{}", 0)], "edge_total": [("{}", 0)], "app_total": [(APP_TOTAL, 0)]}),
    "수집하지_못하면_0이_아니라_No_data다": ("10m", {
        'up{job="prometheus"}': "1x20",
    }, {"edge_rate": [], "edge_total": [], "app_rate": [], "app_total": [], "api_table": []}),
    "수집되는_중_차단이_없으면_규칙별_패널은_증가_없음_0선이다": ("10m", {
        APP_ALLOWED: "0+3000x20",
    }, {"app_rate": [(APP_NO_BLOCK, 0)]}),
    "차단_규칙선은_그_앱의_0선만_대체하고_다른_앱은_0선을_유지한다": ("10m", {
        APP_BLOCKED: "0+240x20", APP_ALLOWED: "0+3000x20", DEV_ALLOWED: "0+3000x20",
    }, {"app_rate": [(APP_RULE, 8), (DEV_NO_BLOCK, 0)]}),
    "429가_없으면_표는_429_증가_없음_행만_돌려준다": ("10m", {
        HTTP_200: "0+1000x20",
    }, {"api_table": [(API_PLACEHOLDER, 0)]}),
    "범위_안에_늘지_않은_429_series는_실제_행이_아니다": ("10m", {
        HTTP_200: "0+1000x20", HTTP_429: "100x20",
    }, {"api_table": [(API_PLACEHOLDER, 0)]}),
    "429가_있으면_표는_실제_행만_돌려주고_429_증가_없음_행은_없다": ("10m", {
        HTTP_200: "0+1000x20", HTTP_429: "0+60x20",
    }, {"api_table": [(API_ROW, 1200)]}),
    "표와_건수는_환경을_합치지_않는다": ("10m", {
        APP_BLOCKED: "0+60x20", DEV_ALLOWED: "0+60x20",
        HTTP_200: "0+1000x20", HTTP_429: "0+60x20", DEV_HTTP_200: "0+1000x20", DEV_HTTP_429: "0+30x20",
    }, {"app_total": [(APP_TOTAL, 1200), (APP_TOTAL.replace("prod-umc-product", "dev-umc-product"), 0)],
        "api_table": [(API_ROW, 1200), (DEV_API_ROW, 600)]}),
    "한_환경만_429가_있으면_다른_환경은_429_증가_없음_행이다": ("10m", {
        HTTP_200: "0+1000x20", HTTP_429: "0+60x20", DEV_HTTP_200: "0+1000x20",
    }, {"api_table": [(API_ROW, 1200), (DEV_API_PLACEHOLDER, 0)]}),
    "한_환경의_429_API가_많아도_다른_환경의_실제_행을_가리지_않는다": ("10m", {
        **{HTTP_429.replace("/api/a", f"/api/p{i}"): "0+100x20" for i in range(10)},
        DEV_HTTP_429: "0+30x20",
    }, {"api_table": [(API_ROW.replace("/api/a", f"/api/p{i}"), 2000.0) for i in range(10)] + [(DEV_API_ROW, 600.0)]}),
    "범위_안에서_처음_생긴_counter의_첫_값은_세지_못한다": ("10m", {
        HTTP_200: "0+1000x20", HTTP_429: "_x10 50x10",
    }, {"api_table": [(API_PLACEHOLDER, 0)]}),
    # docs/operations/domains-tls.md에 적은 한계다. Traefik 재시작 뒤 앱이 처음 429를 돌려주면 새 service series의 첫 수집값
    # (30초 동안의 앱 429 300건) 중 외삽이 메우지 못한 155건이 edge로 넘친다. 실제 edge 차단은 0건이다.
    "재시작_뒤_첫_앱_429_구간은_edge로_넘친다": ("6m", {
        ENTRYPOINT_429: "5x10 305+300x9", SERVICE_429: "_x11 300+300x9",
    }, {"edge_rate": [("{}", 1.292)], "edge_total": [("{}", 155)]}),
}


# 429 패널 쿼리를 대시보드에서 그대로 꺼내 promtool에 가상 series를 넣고 값을 확인한다.
class RateLimitPanelsTest(unittest.TestCase):
    def setUp(self) -> None:
        promtool = shutil.which("promtool")
        if promtool is None:
            self.skipTest("promtool not installed; 429 panel behaviour tests skipped")
        self.promtool = promtool
        self.exprs = {}
        for filename, panels in RATE_LIMIT_PANELS.items():
            path = Path(__file__).resolve().parents[2] / "observability/dashboards" / filename
            dashboard = json.loads(path.read_text(encoding="utf-8"))
            variables = {**dashboard_variables(dashboard), **PANEL_VARIABLES}
            found = {panels[(panel, ref)]: substitute_variables(expr, variables)
                     for panel, ref, expr in raw_queries(dashboard) if (panel, ref) in panels}
            self.assertEqual(set(found), set(panels.values()), filename)
            self.exprs.update(found)

    def test_429_패널_값(self) -> None:
        for name, (eval_time, series, expected) in RATE_LIMIT_SCENARIOS.items():
            test = {"interval": "30s",
                    "input_series": [{"series": key, "values": values} for key, values in series.items()],
                    "promql_expr_test": [
                        {"expr": f"round(({self.exprs[query]}), 0.001)", "eval_time": eval_time,
                         "exp_samples": [{"labels": labels, "value": value} for labels, value in samples]}
                        for query, samples in expected.items()]}
            with self.subTest(name), tempfile.TemporaryDirectory() as directory:
                path = Path(directory, "panels.test.json")
                path.write_text(json.dumps({"rule_files": [], "tests": [test]}), encoding="utf-8")
                result = subprocess.run([self.promtool, "test", "rules", str(path)],
                                        capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()

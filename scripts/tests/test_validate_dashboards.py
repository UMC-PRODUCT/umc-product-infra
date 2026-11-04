#!/usr/bin/env python3
"""dashboard 패널 쿼리 검사가 운영에 없는 메트릭과 깨진 PromQL을 잡는지 확인한다."""

from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from validate_dashboards import (  # noqa: E402
    BUILTIN_VARIABLES,
    PanelQuery,
    check_metric_names,
    check_promql,
    dashboard_variables,
    deployed_dashboards,
    metric_names,
    panel_queries,
    read_metric_names,
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


# 429 패널이 두 출처를 섞거나 Traefik이 전달만 한 앱 429를 edge 차단으로 세면 실패해야 한다.
class RateLimitPanelsTest(unittest.TestCase):
    def setUp(self) -> None:
        dashboard = Path(__file__).resolve().parents[2] / "observability/dashboards/server-default.json"
        self.queries = [query for query in panel_queries(dashboard)
                        if "api_rate_limit_requests_total" in query.expr or "traefik_" in query.expr]

    def test_앱_limiter와_traefik_edge를_따로_센다(self) -> None:
        self.assertEqual(len(self.queries), 4, self.queries)
        for query in self.queries:
            with self.subTest(query.location):
                if "api_rate_limit_requests_total" in query.expr:
                    self.assertIn('result="blocked"', query.expr)
                    self.assertNotIn("traefik_", query.expr)
                else:
                    # rateLimit·inFlightReq가 만든 429만 남기려면 앱이 돌려준 service 429를 빼야 한다.
                    self.assertIn('traefik_entrypoint_requests_total{code="429"}', query.expr)
                    self.assertIn('- (sum(', query.expr)
                    self.assertIn('traefik_service_requests_total{code="429"}', query.expr)

    def test_수집되지_않으면_0이_아니라_no_data다(self) -> None:
        # 0은 기준 counter가 수집되는 동안에만 만든다. 바깥에 맨 vector(0)을 두면 수집 실패가 0건으로 보인다.
        for query in self.queries:
            with self.subTest(query.location):
                self.assertFalse(query.expr.rstrip().endswith("vector(0)"), query.expr)
                if "increase(" in query.expr:
                    self.assertTrue(query.expr.endswith("* 0"), query.expr)


if __name__ == "__main__":
    unittest.main()

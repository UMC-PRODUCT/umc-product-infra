#!/usr/bin/env python3
"""API 제거 검토 화면이 미관측을 0으로 숨기거나 사용자 정보를 노출하지 않게 한다."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class ApiLifecycleDashboardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        dashboard = json.loads(
            (ROOT / "observability/dashboards/api-lifecycle.json").read_text(encoding="utf-8")
        )
        cls.spec = dashboard["spec"]
        cls.panels = [element["spec"] for element in cls.spec["elements"].values()]
        cls.loki_queries = [
            query["spec"]["query"]["spec"]
            for panel in cls.panels
            for query in panel["data"]["spec"]["queries"]
            if query["spec"]["query"]["group"] == "loki"
        ]

    def test_미관측을_호출_0건으로_보충하지_않는다(self) -> None:
        for query in self.loki_queries:
            with self.subTest(expr=query["expr"]):
                self.assertNotIn("vector(0)", query["expr"])
                self.assertNotIn("| json", query["expr"])
                self.assertNotIn("| logfmt", query["expr"])
                # 버전은 stream selector가 아닌 structured metadata로 조회한다.
                selector = re.search(r'\{(?:[^"{}]|"[^"]*")*\}', query["expr"])
                self.assertIsNotNone(selector)
                self.assertNotIn("clientVersion", selector.group(0))

    def test_희귀한_버전을_상위_목록에서_잘라내지_않는다(self) -> None:
        versions = [query for query in self.loki_queries
                    if "sum by (clientService, clientVersion)" in query["expr"]]
        self.assertEqual(len(versions), 1)
        self.assertNotIn("topk(", versions[0]["expr"])
        panel = next(p for p in self.panels
                     if p["data"]["spec"]["queries"] and
                     "sum by (clientService, clientVersion)" in str(p["data"]["spec"]["queries"]))
        # Grafana All values의 기본 25행 제한으로 드문 버전이 잘리지 않게 한다.
        self.assertGreaterEqual(panel["vizConfig"]["spec"]["options"]["reduceOptions"].get("limit", 25), 500)

    def test_계측전과_헤더누락의_분포를_구분한다(self) -> None:
        states = [query for query in self.loki_queries
                  if "sum by (clientVersionStatus)" in query["expr"]]
        self.assertEqual(len(states), 1)
        self.assertIn("not_instrumented", states[0]["expr"])
        self.assertIn("{{ if .clientVersionStatus }}{{ .clientVersionStatus }}", states[0]["expr"])

    def test_기본_7일_관측을_유지하되_자동반복_조회는_하지_않는다(self) -> None:
        window = next(v["spec"] for v in self.spec["variables"] if v["spec"]["name"] == "window")
        self.assertIn("1h", {o["value"] for o in window["options"]})
        self.assertEqual(window["current"]["value"], "7d")
        self.assertEqual(self.spec["timeSettings"]["from"], "now-7d")
        self.assertFalse(window["allowCustomValue"])
        self.assertEqual(self.spec["timeSettings"]["autoRefresh"], "")
        for panel in self.panels:
            if panel["data"]["spec"]["queries"]:
                group = panel["data"]["spec"]["queries"][0]["spec"]["query"]["group"]
                expected = "$window" if group == "loki" else "5m"
                self.assertEqual(panel["data"]["spec"]["queryOptions"]["timeFrom"], expected)

    def test_집계에_불필요한_메타데이터로_중간_시계열을_늘리지_않는다(self) -> None:
        for query in self.loki_queries:
            expr = query["expr"]
            if "count_over_time" not in expr:
                continue
            grouped = re.search(r"sum by \(([^)]+)\)", expr)
            required = {s.strip() for s in grouped.group(1).split(",")} if grouped else set()
            kept = re.search(r"\| keep ([^[]+) \[", expr)
            self.assertIsNotNone(kept)
            self.assertEqual({s.strip() for s in kept.group(1).split(",")},
                             required | {"service_name"})

    def test_순간조회_표의_모든_API와_버전_행을_표시한다(self) -> None:
        # Loki instant 결과는 표로 변환된다. 마지막 행만 reduce하면 API/버전이 사라진다.
        for panel in self.panels:
            if panel["vizConfig"]["group"] in {"bargauge", "piechart"}:
                with self.subTest(panel=panel["title"]):
                    self.assertTrue(panel["vizConfig"]["spec"]["options"]["reduceOptions"]["values"])

    def test_최근로그에_개인정보를_반환하지_않는다(self) -> None:
        recent = next(p for p in self.panels if p["vizConfig"]["group"] == "logs")
        query = recent["data"]["spec"]["queries"][0]["spec"]["query"]["spec"]
        self.assertEqual(query["direction"], "backward")
        self.assertLessEqual(query["maxLines"], 200)
        self.assertFalse(recent["vizConfig"]["spec"]["options"]["enableLogDetails"])
        kept = re.search(r"\| keep ([^|]+)\| line_format", query["expr"])
        self.assertIsNotNone(kept)
        self.assertEqual(
            {name.strip() for name in kept.group(1).split(",")},
            {"service_name", "method", "uriTemplate", "clientService", "clientVersion",
             "clientVersionStatus", "statusCode"},
        )
        self.assertNotIn("__line__", query["expr"])
        for sensitive in (".memberId", ".path", ".clientIp", ".traceId"):
            self.assertNotIn(sensitive, query["expr"])


if __name__ == "__main__":
    unittest.main()

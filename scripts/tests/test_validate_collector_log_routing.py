#!/usr/bin/env python3
"""Regression checks for keeping Preview logs out of production Loki pushes."""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from validate_observability import APPLICATION_DIR, PREVIEW_SERVICE_NAME, ROOT, validate_collector_config


def without_nulls(value):
    if isinstance(value, dict):
        return {key: without_nulls(item) for key, item in value.items() if item is not None}
    return value


# Helm은 values의 null 키를 chart 기본값에서 지운다. 렌더된 relay 설정과 같은 모양으로 맞춘다.
def collector_config() -> dict:
    application = yaml.safe_load((APPLICATION_DIR / "otel-collector.yaml").read_text())
    return without_nulls(application["spec"]["source"]["helm"]["valuesObject"]["config"])


# Preview 로그가 운영·dev 경로로 새거나 운영·dev가 줄어드는 변경은 실패해야 한다.
class CollectorLogRoutingTest(unittest.TestCase):
    def test_현재_Collector_설정은_Preview_로그를_분리한다(self) -> None:
        validate_collector_config(collector_config())

    def test_Preview_service_name만_Preview_경로로_간다(self) -> None:
        deployment = (ROOT / "charts/umc-product-server/templates/deployment.yaml").read_text()
        self.assertIn(f'regexMatch "{PREVIEW_SERVICE_NAME}" $applicationName', deployment)
        self.assertRegex("preview-umc-product-pr42", PREVIEW_SERVICE_NAME)
        for service in ("prod-umc-product", "dev-umc-product", "preview-umc-product-pr42-x",
                        "preview-umc-product-bootstrap-required"):
            self.assertNotRegex(service, PREVIEW_SERVICE_NAME)

    def test_Preview_격리를_깨는_변경을_거부한다(self) -> None:
        def route(config: dict) -> dict:
            return config["connectors"]["routing/logs"]

        def pipelines(config: dict) -> dict:
            return config["service"]["pipelines"]

        def preview_queue(config: dict) -> dict:
            return config["exporters"]["otlphttp/loki-preview"]["sending_queue"]

        def sampler(config: dict) -> dict:
            return config["processors"]["probabilistic_sampler/preview"]

        mutations = {
            "copy action leaks Preview to default": (
                lambda c: route(c)["table"][0].update(action="copy"), "routing/logs"),
            "route removed": (lambda c: route(c).update(table=[]), "routing/logs"),
            "unanchored regex": (lambda c: route(c)["table"][0].update(
                condition='IsMatch(attributes["service.name"], "preview-umc-product-pr")'), "routing/logs"),
            "Preview also default": (
                lambda c: route(c).update(default_pipelines=["logs/default", "logs/preview"]), "routing/logs"),
            "OTLP bypasses routing": (lambda c: pipelines(c).update({"logs/raw": {
                "receivers": ["otlp"], "processors": ["memory_limiter"], "exporters": ["otlphttp/loki"]}}),
                "pipeline set"),
            "shared Loki exporter queue": (
                lambda c: pipelines(c)["logs/preview"].update(exporters=["otlphttp/loki"]), "logs/preview"),
            "default sampled": (lambda c: pipelines(c)["logs/default"].update(
                processors=["memory_limiter", "probabilistic_sampler/preview", "batch"]),
                "logs/default processors must be exactly .*COLLECTOR_PIPELINES"),
            "memory_limiter not first": (lambda c: pipelines(c)["logs/preview"].update(
                processors=["filter/preview", "memory_limiter", "probabilistic_sampler/preview", "batch"]),
                "logs/preview processors must be exactly"),
            "Preview unsampled": (lambda c: sampler(c).update(sampling_percentage=100), "below 100%"),
            "Preview drops logs without trace": (lambda c: sampler(c).update(fail_closed=True), "fail_closed"),
            "Preview sampler default fail_closed": (lambda c: sampler(c).pop("fail_closed"), "fail_closed"),
            "Preview INFO kept": (
                lambda c: c["processors"]["filter/preview"].update(log_conditions=[]), "below WARN"),
            "Preview queue unbounded": (
                lambda c: c["exporters"]["otlphttp/loki-preview"].pop("sending_queue"), "bounded in bytes"),
            "Preview queue disabled": (lambda c: preview_queue(c).update(enabled=False), "stay enabled"),
            "Preview queue blocks": (
                lambda c: preview_queue(c).update(block_on_overflow=True), "block_on_overflow"),
            "Preview queue size as string": (
                lambda c: preview_queue(c).update(queue_size="32MiB"), "integer queue_size"),
            "Preview sent elsewhere": (lambda c: c["exporters"]["otlphttp/loki-preview"].update(
                endpoint="http://loki-preview:3100/otlp"), "Loki OTLP endpoint"),
            "Preview exporter missing": (
                lambda c: c["exporters"].pop("otlphttp/loki-preview"), "exporter set"),
            "Prometheus exporter missing": (
                lambda c: c["exporters"].pop("otlphttp/prometheus"), "exporter set"),
            "metrics sent elsewhere": (lambda c: c["exporters"]["otlphttp/prometheus"].update(
                endpoint="http://prometheus:9090/api/v1/otlp"), "Prometheus OTLP receiver"),
            "extra receiver": (lambda c: c["receivers"].update(zipkin={}), "receiver set"),
        }
        for name, (mutate, message) in mutations.items():
            broken = copy.deepcopy(collector_config())
            mutate(broken)
            with self.subTest(name), self.assertRaisesRegex(SystemExit, message):
                validate_collector_config(broken)


if __name__ == "__main__":
    unittest.main()

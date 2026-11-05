#!/usr/bin/env python3
"""생성한 dashboard ConfigMap이 Grafana sidecar가 기대하는 folder로 들어가는지 확인한다."""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest

import yaml


ROOT = pathlib.Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("gen_configmaps", ROOT / "scripts/gen-configmaps.py")
assert SPEC is not None and SPEC.loader is not None
gen_configmaps = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gen_configmaps
SPEC.loader.exec_module(gen_configmaps)


def argocd_values(name: str) -> dict:
    application = yaml.safe_load(
        (ROOT / f"argocd/applications/platform/observability/{name}.yaml").read_text(encoding="utf-8")
    )
    return application["spec"]["source"]["helm"]["valuesObject"]


class DashboardFolderTest(unittest.TestCase):
    outputs: dict[str, dict]

    @classmethod
    def setUpClass(cls) -> None:
        cls.outputs = {
            path.name: yaml.safe_load(content)
            for path, content in gen_configmaps.desired_outputs(ROOT / "observability").items()
        }

    def folder_annotation(self, filename: str) -> str:
        configmap = self.outputs[pathlib.Path(filename).with_suffix(".yaml").name]
        self.assertEqual(configmap["metadata"]["labels"]["grafana_dashboard"], "1")
        return configmap["metadata"]["annotations"]["grafana_folder"]

    def test_kubernetes_guide_shares_the_chart_dashboard_folder(self) -> None:
        chart_folder = argocd_values("prometheus")["grafana"]["sidecar"]["dashboards"]["annotations"][
            "grafana_folder"
        ]
        self.assertEqual(self.folder_annotation("kubernetes-guide.json"), chart_folder)

    def test_umc_dashboards_stay_in_the_home_dashboard_folder(self) -> None:
        home = argocd_values("grafana")["grafana.ini"]["dashboards"]["default_home_dashboard_path"]
        home_folder = str(pathlib.PurePosixPath(home).parent)
        umc_dashboards = [
            filename
            for filename, folder in gen_configmaps.DASHBOARD_ALLOWLIST.items()
            if folder == gen_configmaps.UMC_PRODUCT_FOLDER
        ]
        self.assertIn("system-overview.json", umc_dashboards)
        for filename in umc_dashboards:
            with self.subTest(filename=filename):
                self.assertEqual(self.folder_annotation(filename), home_folder)

    def test_rule_configmap_is_not_loaded_as_a_dashboard(self) -> None:
        metadata = self.outputs["loki-alert-rules.yaml"]["metadata"]
        self.assertNotIn("grafana_dashboard", metadata["labels"])
        self.assertNotIn("annotations", metadata)


if __name__ == "__main__":
    unittest.main()

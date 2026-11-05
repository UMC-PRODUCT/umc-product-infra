#!/usr/bin/env python3
"""Validate UMC-specific contracts that generic YAML/schema tools cannot express."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from contracts import common
from contracts.aws import (
    validate_app_storage_contract,
    validate_cloudformation_tags,
    validate_ses_contract,
)
from contracts.common import require
from contracts.dns import validate_route53_contract
from contracts.edge import (
    validate_active_edge_fixture,
    validate_active_preview_edge_fixture,
)
from contracts.external_secrets import validate_secrets
from contracts.postgres import validate_postgres
from contracts.preview_budget import validate_preview_budget
from contracts.repository import validate_repository_identity
from contracts.server_app import validate_reloader_argocd_contract, validate_render


# 정적 원본 계약을 먼저 검사한 뒤 환경별 렌더와 합성 공개 경로의 교차 검증을 수행한다.
def main() -> int:
    if len(sys.argv) != 9:
        raise SystemExit(
            "usage: validate_contracts.py PROD_RENDER DEV_RENDER PREVIEW_RENDER "
            "SECRETS_RENDER ACTIVE_EDGE_FIXTURE ACTIVE_PREVIEW_EDGE_FIXTURE "
            "PREVIEW_APPLICATIONSET_RENDER GRAFANA_RENDER"
        )
    validate_repository_identity()
    validate_route53_contract([Path(filename) for filename in sys.argv[1:]])
    validate_app_storage_contract()
    validate_ses_contract()
    validate_cloudformation_tags()
    validate_postgres()
    validate_preview_budget()
    validate_reloader_argocd_contract()
    base_values = yaml.safe_load(
        (common.ROOT / "charts" / "umc-product-server" / "values.yaml").read_text(
            encoding="utf-8"
        )
    )
    require(
        base_values.get("testApi")
        == {
            "enabled": False,
            "basicAuth": {"existingSecret": "docs-basic-auth"},
        },
        "test API base values must be disabled and reuse docs-basic-auth",
    )
    for environment, filename in zip(("prod", "dev", "preview"), sys.argv[1:4], strict=True):
        overlay = yaml.safe_load(
            (
                common.ROOT
                / "charts"
                / "umc-product-server"
                / f"values-{environment}.yaml"
            ).read_text(encoding="utf-8")
        )
        ingress_enabled = overlay["ingress"]["enabled"]
        require(
            isinstance(ingress_enabled, bool),
            f"{environment}: ingress.enabled must be boolean",
        )
        test_api_enabled = overlay.get("testApi", {}).get("enabled", False)
        require(
            isinstance(test_api_enabled, bool),
            f"{environment}: testApi.enabled must be boolean",
        )
        require(
            test_api_enabled is (environment in {"dev", "preview"}),
            f"{environment}: test API must be enabled only in dev and preview",
        )
        validate_render(
            Path(filename),
            environment,
            ingress_enabled,
            test_api_enabled,
        )
    validate_secrets(Path(sys.argv[4]))
    validate_active_edge_fixture(Path(sys.argv[5]))
    validate_active_preview_edge_fixture(Path(sys.argv[6]))
    print("UMC infrastructure contracts are valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

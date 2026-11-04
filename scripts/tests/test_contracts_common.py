#!/usr/bin/env python3
"""Tests that every contract module reads the repository root from contracts.common."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from contracts.aws import validate_app_storage_contract, validate_ses_contract  # noqa: E402
from contracts.dns import external_dns_registry_config, validate_route53_contract  # noqa: E402
from contracts.postgres import validate_postgres  # noqa: E402
from contracts.preview_budget import validate_preview_budget  # noqa: E402
from contracts.repository import validate_repository_identity  # noqa: E402
from contracts.server_app import validate_reloader_argocd_contract  # noqa: E402


# 모듈마다 ROOT를 따로 patch해야 하면 하나를 빠뜨린 테스트가 실제 저장소를 조용히 검사한다.
class SharedRootTest(unittest.TestCase):
    def test_common_root_patch_redirects_every_module(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch("contracts.common.ROOT", Path(directory)):
            for check in (
                validate_app_storage_contract,
                validate_ses_contract,
                external_dns_registry_config,
                lambda: validate_route53_contract([]),
                validate_postgres,
                validate_preview_budget,
                validate_reloader_argocd_contract,
            ):
                with self.subTest(check=getattr(check, "__name__", "validate_route53_contract")):
                    with self.assertRaises(FileNotFoundError) as raised:
                        check()
                    self.assertTrue(str(raised.exception.filename).startswith(directory))
            with self.assertRaisesRegex(SystemExit, "missing directory: ansible"):
                validate_repository_identity()


if __name__ == "__main__":
    unittest.main()

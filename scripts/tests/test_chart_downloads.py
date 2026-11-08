from __future__ import annotations

import hashlib
from email.message import Message
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chart_downloads import download_release_asset, template_chart
import validate_argocd


URL = "https://github.com/example/charts/releases/download/chart-1.2.3/chart-1.2.3.tgz"
ASSET_URL = "https://api.github.com/repos/example/charts/releases/assets/123"
PAYLOAD = b"pinned chart archive"


class ChartDownloadTests(unittest.TestCase):
    def release(self, digest: str | None = None) -> io.BytesIO:
        return io.BytesIO(json.dumps({"assets": [
            {"name": "other.tgz", "url": "https://example.invalid/other"},
            {"name": "chart-1.2.3.tgz", "url": ASSET_URL, "digest": digest},
        ]}).encode())

    def test_api_download_selects_exact_release_and_asset(self) -> None:
        digest = "sha256:" + hashlib.sha256(PAYLOAD).hexdigest()
        with tempfile.TemporaryDirectory() as directory, patch(
            "chart_downloads.urllib.request.urlopen",
            side_effect=[self.release(digest), io.BytesIO(PAYLOAD)],
        ) as opened:
            destination = Path(directory) / "chart.tgz"
            download_release_asset(URL, destination)
            self.assertEqual(destination.read_bytes(), PAYLOAD)
            metadata_request = opened.call_args_list[0].args[0]
            asset_request = opened.call_args_list[1].args[0]
            self.assertEqual(metadata_request.full_url,
                             "https://api.github.com/repos/example/charts/releases/tags/chart-1.2.3")
            self.assertEqual(asset_request.full_url, ASSET_URL)
            self.assertEqual(asset_request.get_header("Accept"), "application/octet-stream")

    def test_api_digest_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "chart_downloads.urllib.request.urlopen",
            side_effect=[self.release("sha256:" + "0" * 64), io.BytesIO(PAYLOAD)],
        ), self.assertRaisesRegex(ValueError, "checksum"):
            download_release_asset(URL, Path(directory) / "chart.tgz")

    def test_api_failure_is_not_hidden(self) -> None:
        error = urllib.error.HTTPError(ASSET_URL, 504, "Gateway Timeout", Message(), None)
        self.addCleanup(error.close)
        with tempfile.TemporaryDirectory() as directory, patch(
            "chart_downloads.urllib.request.urlopen", side_effect=error,
        ), self.assertRaises(urllib.error.HTTPError):
            download_release_asset(URL, Path(directory) / "chart.tgz")

    def test_argocd_504_fallback_still_checks_pinned_checksum(self) -> None:
        error = urllib.error.HTTPError(URL, 504, "Gateway Timeout", Message(), None)
        self.addCleanup(error.close)

        def downloaded(url: str, destination: Path) -> None:
            self.assertEqual(url, validate_argocd.load_defaults()["argocd_helm_chart_url"])
            destination.write_bytes(PAYLOAD)

        with tempfile.TemporaryDirectory() as directory, patch(
            "validate_argocd.verify_helm_version",
        ), patch("validate_argocd.urllib.request.urlopen", side_effect=error), patch(
            "validate_argocd.download_release_asset", side_effect=downloaded,
        ) as download, self.assertRaisesRegex(SystemExit, "chart archive checksum"):
            try:
                validate_argocd.main(Path(directory))
            finally:
                download.assert_called_once()

    def command(self) -> list[str]:
        return ["helm", "template", "release", "chart", "--repo", "https://example.com/charts",
                "--version", "1.2.3", "--namespace", "monitoring", "--include-crds", "-f", "-"]

    def test_helm_success_uses_original_command(self) -> None:
        success = subprocess.CompletedProcess(self.command(), 0, "rendered", "")
        with patch("chart_downloads.subprocess.run", return_value=success) as run, patch(
            "chart_downloads.download_release_asset",
        ) as download:
            self.assertIs(template_chart(self.command(), "values: original"), success)
            run.assert_called_once_with(self.command(), input="values: original", text=True,
                                        capture_output=True, check=False)
            download.assert_not_called()

    def test_helm_504_retries_same_archive_with_original_values_and_flags(self) -> None:
        failure = subprocess.CompletedProcess(self.command(), 1, "",
                                               f"Error: failed to fetch {URL} : 504 Gateway Time-out")

        def downloaded(url: str, destination: Path) -> None:
            self.assertEqual(url, URL)
            destination.write_bytes(PAYLOAD)

        def rendered(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if command == self.command():
                return failure
            self.assertEqual(Path(command[3]).read_bytes(), PAYLOAD)
            self.assertEqual(command[:3], self.command()[:3])
            self.assertEqual(command[4:], self.command()[8:])
            self.assertEqual(kwargs["input"], "values: original")
            return subprocess.CompletedProcess(command, 0, "rendered", "")

        with patch("chart_downloads.download_release_asset", side_effect=downloaded), patch(
            "chart_downloads.subprocess.run", side_effect=rendered,
        ) as run:
            self.assertEqual(template_chart(self.command(), "values: original").stdout, "rendered")
            self.assertEqual(run.call_count, 2)

    def test_chart_validation_errors_do_not_trigger_download_fallback(self) -> None:
        failure = subprocess.CompletedProcess(self.command(), 1, "", "Error: invalid values")
        with patch("chart_downloads.subprocess.run", return_value=failure) as run, patch(
            "chart_downloads.download_release_asset",
        ) as download:
            self.assertIs(template_chart(self.command(), "values: original"), failure)
            self.assertEqual(run.call_count, 1)
            download.assert_not_called()


if __name__ == "__main__":
    unittest.main()

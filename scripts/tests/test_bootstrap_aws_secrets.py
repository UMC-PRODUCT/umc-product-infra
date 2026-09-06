from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

import bootstrap_aws_secrets as uploader  # noqa: E402


def valid_worksheets() -> dict[str, dict[str, str]]:
    values = {
        env_file: {
            key: f"value:{env_file}:{key}"
            for key in uploader.expected_env_keys(env_file)
        }
        for env_file in uploader.ENV_FILES
    }
    for env_file in uploader.ENV_FILES:
        values[env_file]["APPLE_WEB_CLIENT_ID"] = ""
        values[env_file]["APPLE_PRIVATE_KEY"] = (
            "-----BEGIN PRIVATE KEY-----\\nexample\\n-----END PRIVATE KEY-----"
        )
    shared_ses = ("shared-nonprod-id", "shared-nonprod-secret")
    for env_file in (".env.dev", ".env.preview"):
        values[env_file]["SES_ACCESS_KEY_ID"] = shared_ses[0]
        values[env_file]["SES_SECRET_ACCESS_KEY"] = shared_ses[1]
    values[".env.prod"]["FIREBASE_CONFIGURATION"] = json.dumps(
        {"type": "service_account", "private_key": "line1\nline2"},
        separators=(",", ":"),
    )
    values[".env.prod"]["BACKUP_AWS_REGION"] = uploader.SEOUL_REGION
    values[".env.prod"]["BACKUP_S3_EXPECTED_BUCKET_OWNER"] = "351284652562"
    values[".env.prod"]["BACKUP_S3_BUCKET"] = (
        "umc-product-prod-postgres-backup-351284652562-ap-northeast-2"
    )
    return values


def serialize_env(env_file: str, values: dict[str, str]) -> str:
    return "# test worksheet\n" + "\n".join(
        f"{key}={values[key]}" for key in sorted(uploader.expected_env_keys(env_file))
    ) + "\n"


class WorksheetParserTests(unittest.TestCase):
    def test_preserves_value_characters_and_allowed_empty_property(self) -> None:
        values = valid_worksheets()[".env.prod"]
        special = "contains=padding#hash$dollar\\nliteral"
        values["KAKAO_CLIENT_SECRET"] = special

        parsed = uploader.parse_env_text(
            serialize_env(".env.prod", values), ".env.prod"
        )

        self.assertEqual(parsed["KAKAO_CLIENT_SECRET"], special)
        self.assertEqual(parsed["APPLE_WEB_CLIENT_ID"], "")

    def test_rejects_duplicate_unknown_and_nonallowed_empty_values(self) -> None:
        values = valid_worksheets()[".env.dev"]
        document = serialize_env(".env.dev", values)
        with self.assertRaises(uploader.BootstrapError):
            uploader.parse_env_text(document + "DATABASE_PASSWORD=again\n", ".env.dev")
        with self.assertRaises(uploader.BootstrapError):
            uploader.parse_env_text(document + "UNKNOWN_KEY=value\n", ".env.dev")
        values["DATABASE_PASSWORD"] = ""
        with self.assertRaises(uploader.BootstrapError):
            uploader.parse_env_text(serialize_env(".env.dev", values), ".env.dev")


class PayloadTests(unittest.TestCase):
    def test_all_sources_and_special_remaps(self) -> None:
        values = valid_worksheets()
        uploader.validate_worksheet_values(values, "351284652562")

        payloads = uploader.build_payloads(values)

        self.assertEqual(len(payloads), 26)
        self.assertIn("APPLE_WEB_CLIENT_ID", payloads["/umc-product/prod/app-oauth"])
        self.assertEqual(
            payloads["/umc-product/prod/app-oauth"]["APPLE_WEB_CLIENT_ID"], ""
        )
        self.assertIsInstance(
            payloads["/umc-product/prod/app-fcm"]["FIREBASE_CONFIGURATION"], str
        )
        self.assertEqual(
            payloads["/umc-product/prod/backup-s3"]["AWS_ACCESS_KEY_ID"],
            values[".env.prod"]["BACKUP_AWS_ACCESS_KEY_ID"],
        )
        self.assertEqual(
            payloads["/umc-product/platform/monitoring/grafana-admin"]["admin-user"],
            values[".env.prod"]["GRAFANA_ADMIN_USER"],
        )
        self.assertEqual(
            payloads[
                "/umc-product/platform/cert-manager/route53-credentials"
            ],
            {
                "access-key-id": values[".env.prod"][
                    "CERT_MANAGER_ROUTE53_ACCESS_KEY_ID"
                ],
                "secret-access-key": values[".env.prod"][
                    "CERT_MANAGER_ROUTE53_SECRET_ACCESS_KEY"
                ],
            },
        )
        self.assertEqual(
            payloads[
                "/umc-product/platform/external-dns/route53-credentials"
            ],
            {
                "access-key-id": values[".env.prod"][
                    "EXTERNAL_DNS_ROUTE53_ACCESS_KEY_ID"
                ],
                "secret-access-key": values[".env.prod"][
                    "EXTERNAL_DNS_ROUTE53_SECRET_ACCESS_KEY"
                ],
            },
        )

    def test_rejects_shared_route53_credentials(self) -> None:
        for cert_manager_key, external_dns_key in (
            (
                "CERT_MANAGER_ROUTE53_ACCESS_KEY_ID",
                "EXTERNAL_DNS_ROUTE53_ACCESS_KEY_ID",
            ),
            (
                "CERT_MANAGER_ROUTE53_SECRET_ACCESS_KEY",
                "EXTERNAL_DNS_ROUTE53_SECRET_ACCESS_KEY",
            ),
        ):
            with self.subTest(external_dns_key=external_dns_key):
                values = valid_worksheets()
                values[".env.prod"][external_dns_key] = values[".env.prod"][
                    cert_manager_key
                ]
                with self.assertRaises(uploader.BootstrapError):
                    uploader.validate_worksheet_values(values, "351284652562")


class AwsTransportTests(unittest.TestCase):
    def test_secret_payload_is_sent_only_through_stdin(self) -> None:
        sentinel = "SECRET_SENTINEL_MUST_NOT_ENTER_ARGV"
        spec = uploader.SecretSpec(
            "/umc-product/test/source",
            ".env.prod",
            (("token", "PREVIEW_GITHUB_TOKEN"),),
            "platform",
        )
        observed: dict[str, object] = {}

        def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            observed["command"] = command
            observed["input"] = kwargs.get("input")
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps({"Name": spec.path}),
                stderr="",
            )

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            uploader.AwsCli("default", uploader.SEOUL_REGION).create_secret(
                spec, json.dumps({"token": sentinel})
            )

        command = observed["command"]
        self.assertIsInstance(command, list)
        self.assertNotIn(sentinel, " ".join(command))
        self.assertIn(sentinel, str(observed["input"]))
        self.assertIn("file:///dev/stdin", command)

    def test_aws_error_does_not_repeat_stderr(self) -> None:
        sentinel = "SECRET_SENTINEL_IN_AWS_ERROR"
        spec = uploader.SecretSpec(
            "/umc-product/test/source",
            ".env.prod",
            (("token", "PREVIEW_GITHUB_TOKEN"),),
            "platform",
        )
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr=sentinel)
        aws = uploader.AwsCli("default", uploader.SEOUL_REGION)
        with mock.patch.object(aws, "run", return_value=failed):
            with self.assertRaises(uploader.BootstrapError) as raised:
                aws.create_secret(spec, json.dumps({"token": sentinel}))
        self.assertNotIn(sentinel, str(raised.exception))


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Validate local worksheets and create the 30 AWS Secrets Manager sources."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SEOUL_REGION = "ap-northeast-2"
ENV_FILES = (".env.prod", ".env.dev", ".env.preview")
ALLOWED_EMPTY_KEYS = {"APPLE_WEB_CLIENT_ID"}
MAX_SECRET_BYTES = 65_536

DATABASE_PROPERTIES = (("DATABASE_PASSWORD", "DATABASE_PASSWORD"),)
JWT_PROPERTIES = (
    ("JWT_ACCESS_TOKEN_SECRET", "JWT_ACCESS_TOKEN_SECRET"),
    ("JWT_REFRESH_TOKEN_SECRET", "JWT_REFRESH_TOKEN_SECRET"),
    ("JWT_OAUTH_VERIFICATION_TOKEN_SECRET", "JWT_OAUTH_VERIFICATION_TOKEN_SECRET"),
    ("JWT_EMAIL_VERIFICATION_TOKEN_SECRET", "JWT_EMAIL_VERIFICATION_TOKEN_SECRET"),
    ("JWT_SSO_LOGIN_TOKEN_SECRET", "JWT_SSO_LOGIN_TOKEN_SECRET"),
    (
        "DEMODAY_STAMP_CREDENTIAL_ENCRYPTION_KEY",
        "DEMODAY_STAMP_CREDENTIAL_ENCRYPTION_KEY",
    ),
    ("DEMODAY_VOTE_QR_SIGNING_KEY", "DEMODAY_VOTE_QR_SIGNING_KEY"),
    (
        "DEMODAY_VOTE_AUTHORIZATION_SIGNING_KEY",
        "DEMODAY_VOTE_AUTHORIZATION_SIGNING_KEY",
    ),
    ("DEMODAY_PARTICIPANT_TOKEN_SECRET", "DEMODAY_PARTICIPANT_TOKEN_SECRET"),
)
OAUTH_PROPERTIES = (
    ("APPLE_IOS_CLIENT_ID", "APPLE_IOS_CLIENT_ID"),
    ("APPLE_WEB_CLIENT_ID", "APPLE_WEB_CLIENT_ID"),
    ("APPLE_TEAM_ID", "APPLE_TEAM_ID"),
    ("APPLE_KEY_ID", "APPLE_KEY_ID"),
    ("APPLE_PRIVATE_KEY", "APPLE_PRIVATE_KEY"),
    ("KAKAO_CLIENT_ID", "KAKAO_CLIENT_ID"),
    ("KAKAO_CLIENT_SECRET", "KAKAO_CLIENT_SECRET"),
)
STORAGE_PROPERTIES = (
    ("S3_ACCESS_KEY_ID", "S3_ACCESS_KEY_ID"),
    ("S3_SECRET_ACCESS_KEY", "S3_SECRET_ACCESS_KEY"),
)
EMAIL_PROPERTIES = (
    ("SES_ACCESS_KEY_ID", "SES_ACCESS_KEY_ID"),
    ("SES_SECRET_ACCESS_KEY", "SES_SECRET_ACCESS_KEY"),
)
SMTP_PROPERTIES = (("SMTP_PASSWORD", "SMTP_PASSWORD"),)
POSTGRES_PROPERTIES = (
    ("POSTGRES_USER", "POSTGRES_USER"),
    ("POSTGRES_PASSWORD", "POSTGRES_PASSWORD"),
)
DOCS_BASIC_AUTH_PROPERTIES = (("users", "DOCS_BASIC_AUTH_USERS"),)


class BootstrapError(RuntimeError):
    """Expected failure whose message never contains a secret value."""


@dataclass(frozen=True)
class SecretSpec:
    path: str
    env_file: str
    properties: tuple[tuple[str, str], ...]
    environment: str


def environment_specs(environment: str) -> tuple[SecretSpec, ...]:
    env_file = f".env.{environment}"
    prefix = f"/umc-product/{environment}"
    postgres_name = (
        "postgres-preview-secrets"
        if environment == "preview"
        else "postgres-secrets"
    )
    return (
        SecretSpec(f"{prefix}/app-db", env_file, DATABASE_PROPERTIES, environment),
        SecretSpec(f"{prefix}/app-jwt", env_file, JWT_PROPERTIES, environment),
        SecretSpec(f"{prefix}/app-oauth", env_file, OAUTH_PROPERTIES, environment),
        SecretSpec(
            f"{prefix}/app-storage", env_file, STORAGE_PROPERTIES, environment
        ),
        SecretSpec(
            f"{prefix}/app-email", env_file,
            EMAIL_PROPERTIES + (SMTP_PROPERTIES if environment in ("prod", "dev") else ()),
            environment,
        ),
        SecretSpec(
            f"{prefix}/{postgres_name}", env_file, POSTGRES_PROPERTIES, environment
        ),
    )


SECRET_SPECS = (
    *environment_specs("prod")[:5],
    SecretSpec(
        "/umc-product/prod/app-fcm",
        ".env.prod",
        (("FIREBASE_CONFIGURATION", "FIREBASE_CONFIGURATION"),),
        "prod",
    ),
    SecretSpec(
        "/umc-product/prod/docs-basic-auth",
        ".env.prod",
        DOCS_BASIC_AUTH_PROPERTIES,
        "prod",
    ),
    *environment_specs("prod")[5:],
    SecretSpec(
        "/umc-product/prod/postgres-readonly",
        ".env.prod",
        (("RO_PASSWORD", "RO_PASSWORD"),),
        "prod",
    ),
    SecretSpec(
        "/umc-product/prod/postgres-exporter",
        ".env.prod",
        (("POSTGRES_EXPORTER_PASSWORD", "POSTGRES_EXPORTER_PASSWORD"),),
        "prod",
    ),
    SecretSpec(
        "/umc-product/prod/backup-s3",
        ".env.prod",
        (
            ("AWS_ACCESS_KEY_ID", "BACKUP_AWS_ACCESS_KEY_ID"),
            ("AWS_SECRET_ACCESS_KEY", "BACKUP_AWS_SECRET_ACCESS_KEY"),
            ("AWS_REGION", "BACKUP_AWS_REGION"),
            ("S3_BUCKET", "BACKUP_S3_BUCKET"),
            ("S3_EXPECTED_BUCKET_OWNER", "BACKUP_S3_EXPECTED_BUCKET_OWNER"),
        ),
        "prod",
    ),
    *environment_specs("dev")[:5],
    SecretSpec(
        "/umc-product/dev/docs-basic-auth",
        ".env.dev",
        DOCS_BASIC_AUTH_PROPERTIES,
        "dev",
    ),
    *environment_specs("dev")[5:],
    *environment_specs("preview"),
    SecretSpec(
        "/umc-product/preview/docs-basic-auth",
        ".env.preview",
        DOCS_BASIC_AUTH_PROPERTIES,
        "preview",
    ),
    SecretSpec(
        "/umc-product/platform/monitoring/grafana-admin",
        ".env.prod",
        (
            ("admin-user", "GRAFANA_ADMIN_USER"),
            ("admin-password", "GRAFANA_ADMIN_PASSWORD"),
        ),
        "platform",
    ),
    SecretSpec(
        "/umc-product/platform/monitoring/alertmanager-discord",
        ".env.prod",
        (("discord-webhook", "ALERTMANAGER_DISCORD_WEBHOOK"),),
        "platform",
    ),
    SecretSpec(
        "/umc-product/platform/argocd/preview-github-token",
        ".env.prod",
        (("token", "PREVIEW_GITHUB_TOKEN"),),
        "platform",
    ),
    SecretSpec(
        "/umc-product/platform/cert-manager/route53-credentials",
        ".env.prod",
        (
            ("access-key-id", "CERT_MANAGER_ROUTE53_ACCESS_KEY_ID"),
            ("secret-access-key", "CERT_MANAGER_ROUTE53_SECRET_ACCESS_KEY"),
        ),
        "platform",
    ),
    SecretSpec(
        "/umc-product/platform/external-dns/route53-credentials",
        ".env.prod",
        (
            ("access-key-id", "EXTERNAL_DNS_ROUTE53_ACCESS_KEY_ID"),
            ("secret-access-key", "EXTERNAL_DNS_ROUTE53_SECRET_ACCESS_KEY"),
        ),
        "platform",
    ),
)


def command_succeeded(command: list[str], cwd: Path) -> bool:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            shell=False,
        )
    except OSError:
        return False
    return result.returncode == 0


def repo_root() -> Path:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            shell=False,
        )
    except OSError as error:
        raise BootstrapError("git is required") from error
    if result.returncode != 0:
        raise BootstrapError("run this script inside the umc-infra Git repository")
    return Path(result.stdout.strip()).resolve()


def expected_env_keys(env_file: str) -> set[str]:
    return {
        env_key
        for spec in SECRET_SPECS
        if spec.env_file == env_file
        for _property_name, env_key in spec.properties
    }


def parse_env_text(contents: str, env_file: str) -> dict[str, str]:
    expected = expected_env_keys(env_file)
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(contents.splitlines(), start=1):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        key, separator, value = raw_line.partition("=")
        if (
            separator != "="
            or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key)
            or key not in expected
        ):
            raise BootstrapError(f"{env_file} has an invalid assignment at line {line_number}")
        if key in values:
            raise BootstrapError(f"{env_file} contains duplicate assignment for {key}")
        if "\x00" in value:
            raise BootstrapError(f"{env_file} {key} contains a NUL byte")
        values[key] = value

    missing = sorted(expected - values.keys())
    if missing:
        raise BootstrapError(
            f"{env_file} is missing required assignments: {', '.join(missing)}"
        )
    for key, value in values.items():
        if value == "" and key not in ALLOWED_EMPTY_KEYS:
            raise BootstrapError(f"{env_file} {key} must not be empty")
    return values


def read_env_file(root: Path, env_file: str) -> dict[str, str]:
    path = root / env_file
    if path.is_symlink():
        raise BootstrapError(f"{env_file} must not be a symlink")
    try:
        initial_stat = path.stat(follow_symlinks=False)
    except FileNotFoundError as error:
        raise BootstrapError(f"{env_file} does not exist") from error
    if not stat.S_ISREG(initial_stat.st_mode):
        raise BootstrapError(f"{env_file} must be a regular file")
    if initial_stat.st_uid != os.getuid():
        raise BootstrapError(f"{env_file} must be owned by the current user")
    if stat.S_IMODE(initial_stat.st_mode) != 0o600:
        raise BootstrapError(f"{env_file} mode must be 0600")
    if not command_succeeded(
        ["git", "check-ignore", "--quiet", "--", env_file], root
    ):
        raise BootstrapError(f"{env_file} must be ignored by Git")
    if command_succeeded(
        ["git", "ls-files", "--error-unmatch", "--", env_file], root
    ):
        raise BootstrapError(f"{env_file} must not be tracked by Git")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise BootstrapError(f"could not safely open {env_file}") from error
    try:
        opened_stat = os.fstat(descriptor)
        if (
            opened_stat.st_dev != initial_stat.st_dev
            or opened_stat.st_ino != initial_stat.st_ino
            or not stat.S_ISREG(opened_stat.st_mode)
            or opened_stat.st_uid != os.getuid()
            or stat.S_IMODE(opened_stat.st_mode) != 0o600
        ):
            raise BootstrapError(f"{env_file} changed while it was being opened")
        with os.fdopen(descriptor, "r", encoding="utf-8", newline="") as stream:
            descriptor = -1
            contents = stream.read()
    except UnicodeDecodeError as error:
        raise BootstrapError(f"{env_file} must be UTF-8") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return parse_env_text(contents, env_file)


def validate_worksheet_values(values: dict[str, dict[str, str]], account_id: str) -> None:
    security_keys = tuple(property_name for property_name, _ in JWT_PROPERTIES)
    security_values = [
        values[env_file][key]
        for env_file in ENV_FILES
        for key in security_keys
    ]
    if len(set(security_values)) != len(security_values):
        raise BootstrapError("JWT and Demoday keys must differ across keys and environments")

    database_values = [
        values[env_file][key]
        for env_file in ENV_FILES
        for key in ("DATABASE_PASSWORD", "POSTGRES_PASSWORD")
    ]
    database_values.append(values[".env.prod"]["RO_PASSWORD"])
    database_values.append(values[".env.prod"]["POSTGRES_EXPORTER_PASSWORD"])
    if len(set(database_values)) != len(database_values):
        raise BootstrapError("database passwords must differ by role and environment")

    docs_users = [
        values[env_file]["DOCS_BASIC_AUTH_USERS"]
        for env_file in ENV_FILES
    ]
    htpasswd_pattern = re.compile(
        r"^umc-docs:\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}$"
    )
    if not all(htpasswd_pattern.fullmatch(entry) for entry in docs_users):
        raise BootstrapError(
            "docs Basic Auth users must contain an umc-docs bcrypt htpasswd entry"
        )
    if len(set(docs_users)) != len(docs_users):
        raise BootstrapError("docs Basic Auth credentials must differ by environment")

    storage_ids = [values[env_file]["S3_ACCESS_KEY_ID"] for env_file in ENV_FILES]
    storage_secrets = [
        values[env_file]["S3_SECRET_ACCESS_KEY"] for env_file in ENV_FILES
    ]
    if len(set(storage_ids)) != 3 or len(set(storage_secrets)) != 3:
        raise BootstrapError("S3 credentials must differ by environment")

    prod_ses = (
        values[".env.prod"]["SES_ACCESS_KEY_ID"],
        values[".env.prod"]["SES_SECRET_ACCESS_KEY"],
    )
    dev_ses = (
        values[".env.dev"]["SES_ACCESS_KEY_ID"],
        values[".env.dev"]["SES_SECRET_ACCESS_KEY"],
    )
    preview_ses = (
        values[".env.preview"]["SES_ACCESS_KEY_ID"],
        values[".env.preview"]["SES_SECRET_ACCESS_KEY"],
    )
    if dev_ses != preview_ses or prod_ses == dev_ses:
        raise BootstrapError("SES credentials must be prod-only and shared only by dev/preview")

    cert_manager_route53 = (
        values[".env.prod"]["CERT_MANAGER_ROUTE53_ACCESS_KEY_ID"],
        values[".env.prod"]["CERT_MANAGER_ROUTE53_SECRET_ACCESS_KEY"],
    )
    external_dns_route53 = (
        values[".env.prod"]["EXTERNAL_DNS_ROUTE53_ACCESS_KEY_ID"],
        values[".env.prod"]["EXTERNAL_DNS_ROUTE53_SECRET_ACCESS_KEY"],
    )
    if (
        cert_manager_route53[0] == external_dns_route53[0]
        or cert_manager_route53[1] == external_dns_route53[1]
    ):
        raise BootstrapError(
            "cert-manager and ExternalDNS Route53 credentials must differ"
        )

    for env_file in ENV_FILES:
        private_key = values[env_file]["APPLE_PRIVATE_KEY"]
        if not (
            private_key.startswith("-----BEGIN PRIVATE KEY-----\\n")
            and private_key.endswith("\\n-----END PRIVATE KEY-----")
        ):
            raise BootstrapError(f"{env_file} APPLE_PRIVATE_KEY format is invalid")

    try:
        firebase = json.loads(values[".env.prod"]["FIREBASE_CONFIGURATION"])
    except json.JSONDecodeError as error:
        raise BootstrapError(".env.prod FIREBASE_CONFIGURATION must be valid JSON") from error
    if not isinstance(firebase, dict) or firebase.get("type") != "service_account":
        raise BootstrapError(".env.prod FIREBASE_CONFIGURATION must be a service account")

    prod = values[".env.prod"]
    expected_backup_bucket = (
        f"umc-product-prod-postgres-backup-{account_id}-{SEOUL_REGION}"
    )
    if prod["BACKUP_AWS_REGION"] != SEOUL_REGION:
        raise BootstrapError("backup region must be ap-northeast-2")
    if prod["BACKUP_S3_EXPECTED_BUCKET_OWNER"] != account_id:
        raise BootstrapError("backup bucket owner must match the expected AWS account")
    if prod["BACKUP_S3_BUCKET"] != expected_backup_bucket:
        raise BootstrapError("backup bucket name does not match the AWS contract")


def build_payloads(
    values: dict[str, dict[str, str]],
) -> dict[str, dict[str, str]]:
    payloads: dict[str, dict[str, str]] = {}
    for spec in SECRET_SPECS:
        payload = {
            property_name: values[spec.env_file][env_key]
            for property_name, env_key in spec.properties
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if not 1 <= len(encoded) <= MAX_SECRET_BYTES:
            raise BootstrapError(f"{spec.path} JSON exceeds the Secrets Manager limit")
        payloads[spec.path] = payload
    return payloads


class AwsCli:
    def __init__(self, profile: str, region: str) -> None:
        self.profile = profile
        self.region = region

    @staticmethod
    def environment() -> dict[str, str]:
        environment = os.environ.copy()
        environment["AWS_PAGER"] = ""
        environment["AWS_CLI_AUTO_PROMPT"] = "off"
        environment["AWS_RETRY_MODE"] = "standard"
        return environment

    def run(
        self, arguments: list[str], *, secret_input: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        command = [
            "aws",
            "--profile",
            self.profile,
            "--region",
            self.region,
            "--no-cli-pager",
            "--output",
            "json",
            *arguments,
        ]
        try:
            return subprocess.run(
                command,
                input=secret_input,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                shell=False,
                env=self.environment(),
            )
        except OSError as error:
            raise BootstrapError("AWS CLI is required") from error

    def json(self, arguments: list[str]) -> dict[str, Any]:
        result = self.run(arguments)
        if result.returncode != 0:
            operation = " ".join(arguments[:2])
            raise BootstrapError(f"AWS {operation} failed")
        try:
            response = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as error:
            raise BootstrapError("AWS CLI returned invalid JSON") from error
        if not isinstance(response, dict):
            raise BootstrapError("AWS CLI returned an unexpected response")
        return response

    def identity(self) -> dict[str, Any]:
        return self.json(["sts", "get-caller-identity"])

    def describe_secret(self, path: str) -> dict[str, Any] | None:
        result = self.run(["secretsmanager", "describe-secret", "--secret-id", path])
        if result.returncode != 0:
            if "ResourceNotFoundException" in result.stderr:
                return None
            raise BootstrapError(f"AWS describe-secret failed for {path}")
        try:
            response = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as error:
            raise BootstrapError(f"AWS returned invalid metadata for {path}") from error
        if not isinstance(response, dict):
            raise BootstrapError(f"AWS returned unexpected metadata for {path}")
        return response

    def get_secret_object(self, path: str) -> dict[str, str]:
        response = self.json(
            ["secretsmanager", "get-secret-value", "--secret-id", path]
        )
        secret_string = response.get("SecretString")
        if not isinstance(secret_string, str):
            raise BootstrapError(f"{path} is not stored as a JSON SecretString")
        try:
            payload = json.loads(secret_string)
        except json.JSONDecodeError as error:
            raise BootstrapError(f"{path} does not contain a JSON object") from error
        if not isinstance(payload, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in payload.items()
        ):
            raise BootstrapError(f"{path} must contain only string JSON properties")
        return payload

    def create_secret(self, spec: SecretSpec, secret_string: str) -> None:
        arguments = [
            "secretsmanager",
            "create-secret",
            "--name",
            spec.path,
            "--description",
            "UMC Product External Secrets source",
            "--client-request-token",
            str(uuid.uuid4()),
            "--secret-string",
            "file:///dev/stdin",
            "--tags",
            "Key=managed-by,Value=umc-infra",
            "Key=purpose,Value=external-secrets-source",
            f"Key=environment,Value={spec.environment}",
        ]
        result = self.run(arguments, secret_input=secret_string)
        if result.returncode != 0:
            raise BootstrapError(f"AWS create-secret failed for {spec.path}")
        try:
            response = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as error:
            raise BootstrapError(f"AWS returned invalid create response for {spec.path}") from error
        if not isinstance(response, dict) or response.get("Name") != spec.path:
            raise BootstrapError(f"AWS returned unexpected create response for {spec.path}")


def validate_identity(aws: AwsCli, expected_account_id: str) -> None:
    identity = aws.identity()
    if identity.get("Account") != expected_account_id:
        raise BootstrapError("authenticated AWS account does not match the expected account")
    arn = str(identity.get("Arn", ""))
    if arn.endswith(":root"):
        raise BootstrapError("root AWS credentials are not allowed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="default")
    parser.add_argument("--region", default=SEOUL_REGION)
    parser.add_argument("--expected-account-id", required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="create missing sources; without this flag the command is read-only",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.region != SEOUL_REGION:
        raise BootstrapError(f"region must be {SEOUL_REGION}")
    if not re.fullmatch(r"\d{12}", args.expected_account_id):
        raise BootstrapError("expected account ID must contain exactly 12 digits")

    root = repo_root()
    aws = AwsCli(args.profile, args.region)
    validate_identity(aws, args.expected_account_id)

    worksheet_values = {
        env_file: read_env_file(root, env_file) for env_file in ENV_FILES
    }
    validate_worksheet_values(worksheet_values, args.expected_account_id)
    payloads = build_payloads(worksheet_values)

    statuses: dict[str, str] = {}
    conflicts: list[str] = []
    for spec in SECRET_SPECS:
        metadata = aws.describe_secret(spec.path)
        if metadata is None:
            statuses[spec.path] = "CREATE" if args.apply else "WOULD_CREATE"
            continue
        if "DeletedDate" in metadata:
            statuses[spec.path] = "CONFLICT"
            conflicts.append(spec.path)
            continue
        if aws.get_secret_object(spec.path) == payloads[spec.path]:
            statuses[spec.path] = "SKIP_MATCHED"
        else:
            statuses[spec.path] = "CONFLICT"
            conflicts.append(spec.path)

    for spec in SECRET_SPECS:
        property_names = ",".join(name for name, _env_key in spec.properties)
        print(f"[{statuses[spec.path]}] {spec.path} properties={property_names}")

    if conflicts:
        raise BootstrapError(
            "existing Secrets Manager sources differ or are pending deletion; no changes made"
        )

    if not args.apply:
        missing_count = sum(status == "WOULD_CREATE" for status in statuses.values())
        matched_count = sum(status == "SKIP_MATCHED" for status in statuses.values())
        print(
            f"Dry-run complete: sources={len(SECRET_SPECS)} "
            f"would_create={missing_count} matched={matched_count} changes=0"
        )
        return 0

    created_count = 0
    for spec in SECRET_SPECS:
        if statuses[spec.path] == "SKIP_MATCHED":
            continue
        canonical_json = json.dumps(
            payloads[spec.path],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        aws.create_secret(spec, canonical_json)
        created_count += 1
        print(f"[CREATED] {spec.path}")
        if aws.get_secret_object(spec.path) != payloads[spec.path]:
            raise BootstrapError(f"post-create verification failed for {spec.path}")

    print(
        f"Apply complete: sources={len(SECRET_SPECS)} "
        f"created={created_count} matched={len(SECRET_SPECS) - created_count}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BootstrapError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)

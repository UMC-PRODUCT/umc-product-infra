"""AWS Secrets Manager to Kubernetes Secret mapping contracts."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from bootstrap_aws_secrets import SECRET_SPECS
from contracts.common import ROOT, CloudFormationLoader, require, yaml_documents


BASE_REQUIRED_SECRETS = ["app-db", "app-jwt", "app-oauth", "app-storage", "app-email"]
# prod·dev만 받는 Secret. preview는 FCM과 신고 봇을 쓰지 않는다.
NONPREVIEW_REQUIRED_SECRETS = [*BASE_REQUIRED_SECRETS, "app-fcm", "app-report-discord"]
# QA 시딩 계정 비밀번호는 시딩 자동 실행을 켠 dev만 받는다.
DEV_REQUIRED_SECRETS = [*NONPREVIEW_REQUIRED_SECRETS, "app-seed"]


# AWS 원본 경로와 Kubernetes Secret의 namespace·키 매핑·보존 정책을 함께 대조한다.
def validate_secrets(path: Path) -> None:
    resources = yaml_documents(path)
    stores = [item for item in resources if item.get("kind") == "SecretStore"]
    external_secrets = [item for item in resources if item.get("kind") == "ExternalSecret"]
    require(len(stores) == 9, f"SecretStore count is {len(stores)}, expected 9")
    require(len(external_secrets) == 39, f"ExternalSecret count is {len(external_secrets)}, expected 39")

    expected_names = {
        "app": set(NONPREVIEW_REQUIRED_SECRETS + ["docs-basic-auth"]),
        "db": {
            "app-db",
            "postgres-secrets",
            "postgres-readonly",
            "postgres-exporter",
            "postgres-backup",
            "backup-s3",
        },
        "dev-app": set(DEV_REQUIRED_SECRETS + ["docs-basic-auth"]),
        "dev-db": {"app-db", "postgres-secrets", "postgres-preview-dump"},
        "preview": set(
            BASE_REQUIRED_SECRETS + ["postgres-preview-secrets", "docs-basic-auth", "dev-db-dump"]
        ),
        "monitoring": {"grafana-admin", "alertmanager-discord"},
        "argocd": {"preview-github-token"},
        "cert-manager": {"route53-credentials"},
        "external-dns": {"route53-credentials"},
    }
    actual_names: dict[str, set[str]] = {}
    for item in external_secrets:
        actual_names.setdefault(item["metadata"]["namespace"], set()).add(
            item["metadata"]["name"]
        )
        target = item["spec"]["target"]
        require(target["creationPolicy"] == "Owner", "ExternalSecret creationPolicy")
        require(target["deletionPolicy"] == "Retain", "ExternalSecret deletionPolicy")
    require(actual_names == expected_names, f"ExternalSecret namespace contract: {actual_names}")

    expected_app_properties = {
        "app-jwt": {
            "JWT_ACCESS_TOKEN_SECRET",
            "JWT_REFRESH_TOKEN_SECRET",
            "JWT_OAUTH_VERIFICATION_TOKEN_SECRET",
            "JWT_EMAIL_VERIFICATION_TOKEN_SECRET",
            "JWT_SSO_LOGIN_TOKEN_SECRET",
            "DEMODAY_STAMP_CREDENTIAL_ENCRYPTION_KEY",
            "DEMODAY_VOTE_QR_SIGNING_KEY",
            "DEMODAY_VOTE_AUTHORIZATION_SIGNING_KEY",
            "DEMODAY_PARTICIPANT_TOKEN_SECRET",
        },
        "app-oauth": {
            "APPLE_IOS_CLIENT_ID",
            "APPLE_WEB_CLIENT_ID",
            "APPLE_TEAM_ID",
            "APPLE_KEY_ID",
            "APPLE_PRIVATE_KEY",
            "KAKAO_CLIENT_ID",
            "KAKAO_CLIENT_SECRET",
        },
        "app-email": {"SES_ACCESS_KEY_ID", "SES_SECRET_ACCESS_KEY"},
        "app-storage": {"S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"},
        "app-fcm": {"FIREBASE_CONFIGURATION"},
        "app-report-discord": {"REPORT_DISCORD_BOT_TOKEN", "REPORT_DISCORD_PUBLIC_KEY", "REPORT_DISCORD_GUILD_ID", "REPORT_DISCORD_CHANNEL_ID", "REPORT_DISCORD_MODERATOR_ROLE_ID"},
        "app-seed": {"APP_SEED_DEFAULT_PASSWORD"},
        "docs-basic-auth": {"users"},
        "postgres-exporter": {"POSTGRES_EXPORTER_PASSWORD"},
        "postgres-backup": {"POSTGRES_BACKUP_PASSWORD"},
    }
    for item in external_secrets:
        name = item["metadata"]["name"]
        if name not in expected_app_properties:
            continue
        properties = {
            datum["secretKey"]: datum["remoteRef"]["property"]
            for datum in item["spec"]["data"]
        }
        expected = {key: key for key in expected_app_properties[name]}
        if name == "app-email":
            expected["SMTP_PASSWORD"] = "SMTP_PASSWORD"
        if name == "app-oauth" and item["metadata"]["namespace"] in {"app", "dev-app"}:
            expected["SSO_SOCIAL_GOOGLE_CLIENT_SECRET"] = "SSO_SOCIAL_GOOGLE_CLIENT_SECRET"
        require(
            properties == expected,
            f"{item['metadata']['namespace']}/{name}: JSON property contract",
        )

    store_prefixes = {
        (item["metadata"]["namespace"], item["metadata"]["name"]):
        item["spec"]["provider"]["aws"]["prefix"]
        for item in stores
    }
    source_properties: dict[str, set[str]] = {}
    for item in external_secrets:
        prefix = store_prefixes[
            (item["metadata"]["namespace"], item["spec"]["secretStoreRef"]["name"])
        ]
        for datum in item["spec"]["data"]:
            source = prefix + datum["remoteRef"]["key"]
            source_properties.setdefault(source, set()).add(
                datum["remoteRef"]["property"]
            )
    sources = set(source_properties)
    iam_text = (ROOT / "cloud" / "aws" / "external-secrets-iam.yaml").read_text(
        encoding="utf-8"
    )
    iam_sources = set(re.findall(r"secret:(/umc-product/[^\"\n]+)-\?{6}", iam_text))
    require(sources == iam_sources, f"Secrets Manager/IAM paths differ: {sources ^ iam_sources}")
    require(len(sources) == 37, f"Secrets Manager source count is {len(sources)}, expected 37")
    iam_template = yaml.load(iam_text, Loader=CloudFormationLoader)
    dev_resources = iam_template["Resources"]["DevRole"]["Properties"]["Policies"][0][
        "PolicyDocument"
    ]["Statement"][0]["Resource"]
    expected_dev_resources = {
        "arn:${AWS::Partition}:secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:"
        + source
        + "-??????"
        for source in sources
        if source.startswith("/umc-product/dev/")
    }
    require(
        set(dev_resources) == expected_dev_resources,
        "DevRole must read only dev Secret sources",
    )

    uploader_properties = {
        spec.path: {property_name for property_name, _env_key in spec.properties}
        for spec in SECRET_SPECS
    }
    require(
        uploader_properties == source_properties,
        "secret uploader paths/properties must match rendered ExternalSecrets",
    )
    require(
        len(SECRET_SPECS) == len(uploader_properties) == 37,
        "secret uploader must contain 37 unique sources",
    )
    renamed_properties = {
        "/umc-product/prod/backup-s3": {
            "AWS_ACCESS_KEY_ID": "BACKUP_AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY": "BACKUP_AWS_SECRET_ACCESS_KEY",
            "AWS_REGION": "BACKUP_AWS_REGION",
            "S3_BUCKET": "BACKUP_S3_BUCKET",
            "S3_EXPECTED_BUCKET_OWNER": "BACKUP_S3_EXPECTED_BUCKET_OWNER",
        },
        "/umc-product/platform/monitoring/grafana-admin": {
            "admin-user": "GRAFANA_ADMIN_USER",
            "admin-password": "GRAFANA_ADMIN_PASSWORD",
        },
        "/umc-product/platform/monitoring/alertmanager-discord": {
            "discord-webhook": "ALERTMANAGER_DISCORD_WEBHOOK",
        },
        "/umc-product/platform/argocd/preview-github-token": {
            "token": "PREVIEW_GITHUB_TOKEN",
        },
        "/umc-product/platform/cert-manager/route53-credentials": {
            "access-key-id": "CERT_MANAGER_ROUTE53_ACCESS_KEY_ID",
            "secret-access-key": "CERT_MANAGER_ROUTE53_SECRET_ACCESS_KEY",
        },
        "/umc-product/platform/external-dns/route53-credentials": {
            "access-key-id": "EXTERNAL_DNS_ROUTE53_ACCESS_KEY_ID",
            "secret-access-key": "EXTERNAL_DNS_ROUTE53_SECRET_ACCESS_KEY",
        },
        "/umc-product/prod/docs-basic-auth": {
            "users": "DOCS_BASIC_AUTH_USERS",
        },
        "/umc-product/dev/docs-basic-auth": {
            "users": "DOCS_BASIC_AUTH_USERS",
        },
        "/umc-product/preview/docs-basic-auth": {
            "users": "DOCS_BASIC_AUTH_USERS",
        },
    }
    for spec in SECRET_SPECS:
        actual_mapping = dict(spec.properties)
        expected_mapping = renamed_properties.get(
            spec.path, {property_name: property_name for property_name in actual_mapping}
        )
        require(
            actual_mapping == expected_mapping,
            f"secret uploader env mapping: {spec.path}",
        )
        expected_env_file = (
            ".env.prod"
            if spec.path.startswith("/umc-product/platform/")
            else f".env.{spec.path.split('/')[2]}"
        )
        require(
            spec.env_file == expected_env_file,
            f"secret uploader worksheet mapping: {spec.path}",
        )
    require("ghcr-pull" not in iam_text, "public GHCR must not have an IAM secret path")
    require("umc-secrets-shared" not in iam_text, "obsolete shared IAM role remains")

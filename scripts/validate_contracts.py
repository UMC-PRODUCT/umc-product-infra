#!/usr/bin/env python3
"""Validate UMC-specific contracts that generic YAML/schema tools cannot express."""

from __future__ import annotations

import ipaddress
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import yaml

from bootstrap_aws_secrets import SECRET_SPECS


ROOT = Path(__file__).resolve().parent.parent
AWS_ACCOUNT_ID = "351284652562"
AWS_REGION = "ap-northeast-2"
POSTGIS_IMAGE = (
    "postgis/postgis:18-3.6@sha256:"
    "60f6ad1d21ea86a67d47780b9a0d1e1d200500f62b19293fa834d0dea80b8677"
)
POSTGRES_EXPORTER_IMAGE = (
    "quay.io/prometheuscommunity/postgres-exporter:v0.20.1@sha256:"
    "ac5ec343104fae0e2d84a27bb8d69b38430a11910c5382cad85d478d2bab713e"
)
APP_IMAGE = (
    "ghcr.io/umc-product/umc-product-server@sha256:"
    + "a" * 64
)
PREVIEW_APP_IMAGE = APP_IMAGE
TEST_NET_EDGE_TARGET = "203.0.113.10"
APP_NAMESPACES = {"prod": "app", "dev": "dev-app", "preview": "preview"}
# validate.sh가 Preview 렌더 견본으로 쓰는 PR 번호다.
PREVIEW_SAMPLE_PR_NUMBER = "42"
CERTIFICATE_ISSUERS = {"letsencrypt-staging", "letsencrypt-production"}
KAKAO_JAVASCRIPT_KEY = "4e79ba06223b60e13ad76bb6aec8f2b9"
GOOGLE_CLIENT_ID_LIST = (
    "882297658822-ag179p23eqhc5lti8ue5cgbf5phekbfh.apps.googleusercontent.com,"
    "882297658822-qaf493b6fu6a9f33artnu435jimkoome.apps.googleusercontent.com,"
    "882297658822-gsl0u8uo78qtt5h0ggkm421sml9c02pd.apps.googleusercontent.com,"
    "882297658822-d47kcffbv3eoms278pak6rcv21lhcei8.apps.googleusercontent.com,"
    "655033275728-f7jpdingakk1f4im8reegjmioth8jvnv.apps.googleusercontent.com"
)
BASE_REQUIRED_SECRETS = ["app-db", "app-jwt", "app-oauth", "app-storage", "app-email"]
# prod·dev만 받는 Secret. preview는 FCM과 Discord 알림을 쓰지 않는다.
NONPREVIEW_REQUIRED_SECRETS = [*BASE_REQUIRED_SECRETS, "app-fcm", "app-webhook"]
BASE_RELOADER_SECRETS = ["app-oauth", "app-storage", "app-email"]
NONPREVIEW_RELOADER_SECRETS = [*BASE_RELOADER_SECRETS, "app-fcm", "app-webhook"]
RELOADER_TRIGGER_ANNOTATION = "secret.reloader.stakater.com/reload"
MAIL_CAPTURE_SMTP_HOST = "smtp-capture.university.neordinary.com"
MAIL_CAPTURE_TLS_SECRET = "preview-mail-capture-loopback-tls"
MAIL_CAPTURE_JAVA_TOOL_OPTIONS = (
    "-Djavax.net.ssl.trustStore=/truststore/cacerts "
    "-Djavax.net.ssl.trustStorePassword=changeit"
)
RELOADER_RUNTIME_POINTER = (
    "/spec/template/metadata/annotations/"
    "reloader.stakater.com~1last-reloaded-from"
)
LEGACY_IDENTIFIERS = (
    "tap" + "ple",
    "tap" + "le",
    "tap" + "plee",
    "umc" + ".it.kr",
    "umc" + "-it-kr",
)


class CloudFormationLoader(yaml.SafeLoader):
    """Load CloudFormation YAML while preserving tagged values as plain data."""


def construct_cloudformation_tag(
    loader: CloudFormationLoader, _tag_suffix: str, node: yaml.Node
) -> object:
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


CloudFormationLoader.add_multi_constructor("!", construct_cloudformation_tag)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"contract validation failed: {message}")


def yaml_documents(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as stream:
        return [document for document in yaml.safe_load_all(stream) if document]


# Git이 관리하거나 무시하지 않은 파일만 검사해 로컬 비밀 원장·백업을 내용 검사에서 제외한다.
def git_visible_files(root: Path) -> list[Path]:
    """Return tracked and untracked files that are not excluded by .gitignore."""
    completed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    require(
        completed.returncode == 0,
        f"git file inventory failed: {os.fsdecode(completed.stderr).strip()}",
    )
    return sorted(
        root / os.fsdecode(relative_path)
        for relative_path in completed.stdout.split(b"\0")
        if relative_path
    )


def external_dns_registry_config() -> dict:
    application = yaml.safe_load(
        (ROOT / "argocd" / "applications" / "platform" / "external-dns.yaml").read_text(
            encoding="utf-8"
        )
    )
    return application["spec"]["source"]["helm"]["valuesObject"]


# ExternalDNS ingress source는 rules·tls host와 hostname annotation을 모두 record로 만든다.
# annotationFilter는 key=value 한 쌍만 지원한다. 다른 selector 문법이면 수집 범위를 판단하지 않고 실패한다.
def external_dns_hosts(documents: list[dict], annotation_filter: str) -> set[str]:
    require(
        re.fullmatch(r"[^=!,()\s]+=[^=!,()\s]+", annotation_filter) is not None,
        f"ExternalDNS annotationFilter must be a single key=value: {annotation_filter}",
    )
    opt_in_key, opt_in_value = annotation_filter.split("=")
    hosts: set[str] = set()
    for document in documents:
        if not isinstance(document, dict) or document.get("kind") != "Ingress":
            continue
        annotations = document.get("metadata", {}).get("annotations") or {}
        if annotations.get(opt_in_key) != opt_in_value:
            continue
        spec = document.get("spec", {})
        hosts.update(rule["host"] for rule in spec.get("rules", []) if rule.get("host"))
        hosts.update(host for tls in spec.get("tls", []) for host in tls.get("hosts", []))
        hostname = annotations.get("external-dns.kubernetes.io/hostname", "")
        hosts.update(name.strip() for name in hostname.split(",") if name.strip())
    return hosts


def iam_string_like(pattern: str, value: str) -> bool:
    expression = "".join(
        ".*" if char == "*" else "." if char == "?" else re.escape(char)
        for char in pattern
    )
    return re.fullmatch(expression, value) is not None


# 렌더된 host에서 ExternalDNS가 쓸 A·TXT 이름을 계산해 IAM 허용 패턴과 양방향으로 대조한다.
# *는 Preview PR 번호 자리에만 허용한다. 견본 PR 번호로 바꾼 패턴은 실제 렌더 이름이어야 한다.
def validate_external_dns_record_names(
    allowed_patterns: list[str],
    hosts: set[str],
    *,
    txt_prefix: str,
    record_types: list[str],
) -> None:
    require(hosts, "ExternalDNS: no opted-in Ingress host was collected")
    record_names = set(hosts) | {
        f"{txt_prefix}{record_type.lower()}-{host}"
        for host in hosts
        for record_type in record_types
        if record_type != "TXT"
    }
    for name in sorted(record_names):
        require(
            any(iam_string_like(pattern, name) for pattern in allowed_patterns),
            f"ExternalDNS Route53 IAM does not allow record name: {name}",
        )
    for pattern in allowed_patterns:
        require(
            pattern.count("*") <= 1
            and pattern.replace("*", PREVIEW_SAMPLE_PR_NUMBER) in record_names,
            "ExternalDNS Route53 IAM pattern is broader than a rendered record "
            f"or no longer used: {pattern}",
        )


# 앱·ApplicationSet·Grafana 렌더와 정적 manifest에서 ExternalDNS가 관리할 host를 모은다.
def external_dns_desired_hosts(render_paths: list[Path], annotation_filter: str) -> set[str]:
    documents = [document for path in render_paths for document in yaml_documents(path)]
    documents += [
        document
        for path in sorted((ROOT / "manifests").rglob("*.yaml"))
        for document in yaml_documents(path)
    ]
    return external_dns_hosts(documents, annotation_filter)


# 기존 위임 zone을 재생성하지 않고 인증서용 TXT와 앱용 DNS 변경 권한을 분리하는지 확인한다.
def validate_route53_contract(render_paths: list[Path]) -> None:
    template_path = ROOT / "cloud" / "aws" / "route53-dns.yaml"
    with template_path.open(encoding="utf-8") as stream:
        template = yaml.load(stream, Loader=CloudFormationLoader)
    require(isinstance(template, dict), "Route53 CloudFormation document")
    require(
        template.get("Parameters", {}).get("DnsZoneName", {}).get("Default")
        == "university.neordinary.com",
        "Route53 delegated zone",
    )
    hosted_zone_parameter = template.get("Parameters", {}).get(
        "Route53HostedZoneId", {}
    )
    require(
        "Default" not in hosted_zone_parameter
        and hosted_zone_parameter.get("Type") == "String"
        and hosted_zone_parameter.get("MinLength") == 3
        and hosted_zone_parameter.get("MaxLength") == 32
        and hosted_zone_parameter.get("AllowedPattern") == "^Z[A-Z0-9]+$",
        "Route53 existing hosted zone ID parameter",
    )
    region_assertions = (
        template.get("Rules", {}).get("SeoulRegionOnly", {}).get("Assertions", [])
    )
    require(
        len(region_assertions) == 1
        and region_assertions[0].get("Assert") == ["AWS::Region", AWS_REGION],
        "Route53 stack region gate",
    )

    resources = template.get("Resources", {})
    require(
        set(resources)
        == {
            "CertManagerRoute53User",
            "ExternalDNSRoute53User",
        },
        "Route53 resource set",
    )
    require(
        all(resource.get("Type") != "AWS::Route53::HostedZone" for resource in resources.values()),
        "Route53 stack must reuse the delegated hosted zone",
    )
    require(
        template.get("Outputs", {}).get("Route53HostedZoneId", {}).get("Value")
        == "Route53HostedZoneId"
        and "Route53NameServers" not in template.get("Outputs", {}),
        "Route53 existing zone output",
    )

    zone_arn = "arn:${AWS::Partition}:route53:::hostedzone/${Route53HostedZoneId}"
    cert_manager_user = resources["CertManagerRoute53User"]
    require(
        cert_manager_user.get("Type") == "AWS::IAM::User"
        and cert_manager_user.get("Properties", {}).get("UserName")
        == "umc-cert-manager-route53"
        and cert_manager_user["Properties"]["Policies"][0].get("PolicyName")
        == "manage-umc-acme-dns-challenges",
        "cert-manager Route53 IAM identity",
    )
    cert_manager_statements = {
        item["Sid"]: item
        for item in cert_manager_user["Properties"]["Policies"][0]["PolicyDocument"][
            "Statement"
        ]
    }
    require(
        cert_manager_statements
        == {
            "ObserveRoute53Changes": {
                "Sid": "ObserveRoute53Changes",
                "Effect": "Allow",
                "Action": "route53:GetChange",
                "Resource": "arn:${AWS::Partition}:route53:::change/*",
            },
            "ReadUmcDnsRecords": {
                "Sid": "ReadUmcDnsRecords",
                "Effect": "Allow",
                "Action": "route53:ListResourceRecordSets",
                "Resource": zone_arn,
            },
            "ManageOnlyUmcAcmeTxtRecords": {
                "Sid": "ManageOnlyUmcAcmeTxtRecords",
                "Effect": "Allow",
                "Action": "route53:ChangeResourceRecordSets",
                "Resource": zone_arn,
                "Condition": {
                    "ForAllValues:StringEquals": {
                        "route53:ChangeResourceRecordSetsActions": [
                            "UPSERT",
                            "DELETE",
                        ],
                        "route53:ChangeResourceRecordSetsNormalizedRecordNames": [
                            "_acme-challenge.${DnsZoneName}",
                            "_acme-challenge.api.${DnsZoneName}",
                            "_acme-challenge.api-dev.${DnsZoneName}",
                            "_acme-challenge.grafana.${DnsZoneName}",
                            "_acme-challenge.argo.${DnsZoneName}",
                        ],
                        "route53:ChangeResourceRecordSetsRecordTypes": ["TXT"],
                    }
                },
            },
        },
        "cert-manager Route53 least-privilege policy",
    )

    external_dns_user = resources["ExternalDNSRoute53User"]
    require(
        external_dns_user.get("Type") == "AWS::IAM::User"
        and external_dns_user.get("Properties", {}).get("UserName")
        == "umc-external-dns-route53"
        and external_dns_user["Properties"]["Policies"][0].get("PolicyName")
        == "manage-umc-ingress-dns-records",
        "ExternalDNS Route53 IAM identity",
    )
    external_dns_statements = {
        item["Sid"]: item
        for item in external_dns_user["Properties"]["Policies"][0]["PolicyDocument"][
            "Statement"
        ]
    }
    external_dns_config = external_dns_registry_config()
    external_dns_name_patterns = (
        external_dns_statements.get("ManageOnlyUmcIngressAndOwnershipRecords", {})
        .get("Condition", {})
        .get("ForAllValues:StringLike", {})
        .get("route53:ChangeResourceRecordSetsNormalizedRecordNames")
    )
    require(
        isinstance(external_dns_name_patterns, list),
        "ExternalDNS Route53 record name allowlist",
    )
    require(
        external_dns_statements
        == {
            "DiscoverHostedZones": {
                "Sid": "DiscoverHostedZones",
                "Effect": "Allow",
                "Action": "route53:ListHostedZones",
                "Resource": "*",
            },
            "ReadUmcDnsRecords": {
                "Sid": "ReadUmcDnsRecords",
                "Effect": "Allow",
                "Action": "route53:ListResourceRecordSets",
                "Resource": zone_arn,
            },
            "ManageOnlyUmcIngressAndOwnershipRecords": {
                "Sid": "ManageOnlyUmcIngressAndOwnershipRecords",
                "Effect": "Allow",
                "Action": "route53:ChangeResourceRecordSets",
                "Resource": zone_arn,
                "Condition": {
                    "ForAllValues:StringEquals": {
                        "route53:ChangeResourceRecordSetsActions": [
                            "CREATE",
                            "UPSERT",
                            "DELETE",
                        ],
                        "route53:ChangeResourceRecordSetsRecordTypes": (
                            external_dns_config["managedRecordTypes"]
                        ),
                    },
                    "ForAllValues:StringLike": {
                        "route53:ChangeResourceRecordSetsNormalizedRecordNames": (
                            external_dns_name_patterns
                        )
                    },
                },
            },
        },
        "ExternalDNS Route53 least-privilege policy",
    )
    zone = template["Parameters"]["DnsZoneName"]["Default"]
    validate_external_dns_record_names(
        [pattern.replace("${DnsZoneName}", zone) for pattern in external_dns_name_patterns],
        external_dns_desired_hosts(render_paths, external_dns_config["annotationFilter"]),
        txt_prefix=external_dns_config["txtPrefix"],
        record_types=external_dns_config["managedRecordTypes"],
    )
    require(
        all(resource.get("Type") != "AWS::IAM::AccessKey" for resource in resources.values()),
        "Route53 access keys must not be stored in CloudFormation",
    )
    require(
        not any(
            marker in output_name.lower()
            for output_name in template.get("Outputs", {})
            for marker in ("accesskey", "secret")
        ),
        "Route53 credentials must not be CloudFormation outputs",
    )


# 환경별 bucket·IAM 사용자·CORS와 앱 설정을 대조해 비운영 앱의 운영 저장소 사용을 막는다.
def validate_app_storage_contract() -> None:
    template_path = ROOT / "cloud" / "aws" / "app-storage-s3.yaml"
    with template_path.open(encoding="utf-8") as stream:
        template = yaml.load(stream, Loader=CloudFormationLoader)
    require(isinstance(template, dict), "app storage CloudFormation document")

    expected_config = {
        "prod": {
            "BucketPrefix": "umc-product-prod-app-storage",
            "UserName": "umc-product-prod-app-storage",
            "PolicyName": "access-production-app-storage",
        },
        "dev": {
            "BucketPrefix": "umc-product-dev-app-storage",
            "UserName": "umc-product-dev-app-storage",
            "PolicyName": "access-development-app-storage",
        },
        "preview": {
            "BucketPrefix": "umc-product-preview-app-storage",
            "UserName": "umc-product-preview-app-storage",
            "PolicyName": "access-preview-app-storage",
        },
    }
    environment_parameter = template["Parameters"]["Environment"]
    require("Default" not in environment_parameter, "app storage Environment must be explicit")
    require(
        environment_parameter["AllowedValues"] == list(expected_config),
        "app storage environment allowlist",
    )
    require(
        template["Mappings"]["EnvironmentConfig"] == expected_config,
        "app storage environment mapping",
    )
    expected_prod_origins = [
        "https://university.neordinary.com",
        "https://admin.university.neordinary.com",
        "https://tech.university.neordinary.com",
        "https://activity.university.neordinary.com",
    ]
    expected_nonprod_origins = [
        *expected_prod_origins,
        "http://localhost:5173",
        "https://dev.activity.university.neordinary.com",
    ]
    expected_cors = {
        "CorsRules": [
            {
                "AllowedHeaders": ["*"],
                "AllowedMethods": ["GET", "PUT", "POST"],
                "AllowedOrigins": [
                    "IsProduction",
                    expected_prod_origins,
                    expected_nonprod_origins,
                ],
                "ExposedHeaders": [
                    "ETag",
                    "x-amz-server-side-encryption",
                    "x-amz-request-id",
                    "x-amz-id-2",
                ],
                "MaxAge": 3600,
            }
        ]
    }
    require(
        template["Conditions"]["IsProduction"] == ["Environment", "prod"],
        "app storage production CORS condition",
    )
    # preview만 테스트 업로드를 30일 뒤 만료한다. prod·dev 파일은 만료하지 않는다.
    bucket_properties = template["Resources"]["AppStorageBucket"]["Properties"]
    abort_multipart = {
        "Id": "abort-incomplete-multipart-uploads",
        "Status": "Enabled",
        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
    }
    require(
        template["Conditions"]["IsPreview"] == ["Environment", "preview"]
        and bucket_properties["LifecycleConfiguration"]
        == {
            "Rules": [
                "IsPreview",
                [
                    abort_multipart,
                    {"Id": "expire-preview-test-uploads", "Status": "Enabled", "ExpirationInDays": 30},
                ],
                [
                    abort_multipart,
                    {
                        "Id": "expire-noncurrent-versions",
                        "Status": "Enabled",
                        "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
                        "ExpiredObjectDeleteMarker": True,
                    },
                ],
            ]
        },
        "app storage lifecycle: only preview uploads expire after 30 days; "
        "prod/dev keep noncurrent versions for 30 days",
    )
    # prod·dev에서 지우거나 덮어쓴 업로드를 이전 버전으로 복구할 수 있어야 한다.
    require(
        bucket_properties.get("VersioningConfiguration")
        == ["IsPreview", "AWS::NoValue", {"Status": "Enabled"}],
        "app storage versioning must be enabled for prod and dev",
    )
    app_policies = template["Resources"]["AppStorageUser"]["Properties"]["Policies"]
    # 앱 자격증명이 유출되거나 오작동해도 이전 버전까지 지우지 못하게 한다.
    for policy in app_policies:
        for statement in policy["PolicyDocument"]["Statement"]:
            actions = statement.get("Action", [])
            actions = [actions] if isinstance(actions, str) else actions
            require(
                "NotAction" not in statement
                and all(
                    "*" not in action and action.lower() != "s3:deleteobjectversion"
                    for action in actions
                ),
                "app storage IAM must not delete object versions "
                "(no s3:DeleteObjectVersion, wildcard actions, or NotAction)",
            )
    # 앱 권한을 바꾸면 이 사본도 함께 고쳐 리뷰에서 변경이 드러나게 한다.
    require(
        app_policies
        == [{
            "PolicyName": ["EnvironmentConfig", "Environment", "PolicyName"],
            "PolicyDocument": {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "ListApplicationStorage",
                        "Effect": "Allow",
                        "Action": "s3:ListBucket",
                        "Resource": "AppStorageBucket.Arn",
                    },
                    {
                        "Sid": "ReadWriteApplicationObjects",
                        "Effect": "Allow",
                        "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                        "Resource": [
                            "${AppStorageBucket.Arn}/public/*",
                            "${AppStorageBucket.Arn}/private/*",
                        ],
                    },
                ],
            },
        }],
        "app storage IAM policy differs from the reviewed copy in validate_contracts.py; "
        "update the copy together with the template change",
    )
    require(
        template["Resources"]["AppStorageBucket"]["Properties"][
            "CorsConfiguration"
        ]
        == expected_cors,
        "app storage environment CORS contract",
    )

    for environment, config in expected_config.items():
        values_path = (
            ROOT / "charts" / "umc-product-server" / f"values-{environment}.yaml"
        )
        with values_path.open(encoding="utf-8") as stream:
            values = yaml.safe_load(stream)
        expected_bucket = (
            f"{config['BucketPrefix']}-{AWS_ACCOUNT_ID}-{AWS_REGION}"
        )
        require(
            values["env"]["S3_BUCKET_NAME"] == expected_bucket,
            f"{environment}: CloudFormation/Helm S3 bucket contract",
        )
        require(
            values["env"]["S3_REGION"] == AWS_REGION,
            f"{environment}: CloudFormation/Helm S3 region contract",
        )


# SES identity·DNS 설정과 환경별 발신자 권한이 같은 메일 발송 계약을 가리키는지 검사한다.
def validate_ses_contract() -> None:
    template_path = ROOT / "cloud" / "aws" / "ses-email.yaml"
    with template_path.open(encoding="utf-8") as stream:
        template = yaml.load(stream, Loader=CloudFormationLoader)
    require(isinstance(template, dict), "SES CloudFormation document")

    parameters = template["Parameters"]
    require(
        parameters["SesDomainIdentity"]["Default"]
        == "university.neordinary.com",
        "SES domain identity",
    )
    require(
        parameters["SesMailFromDomain"]["Default"]
        == "mail.university.neordinary.com",
        "SES custom MAIL FROM domain",
    )
    require(
        parameters["SenderEmailAddress"]["Default"]
        == "no-reply@university.neordinary.com",
        "SES production From address",
    )
    require(
        parameters["NonprodSenderEmailAddress"]["Default"]
        == "no-reply-nonprod@university.neordinary.com",
        "SES nonprod From address",
    )
    route53_parameter = parameters["Route53HostedZoneId"]
    require("Default" not in route53_parameter, "SES Route53 Hosted Zone ID must be explicit")
    require(
        route53_parameter.get("Type") == "String"
        and route53_parameter.get("MinLength") == 3
        and route53_parameter.get("MaxLength") == 32
        and route53_parameter.get("AllowedPattern") == "^Z[A-Z0-9]+$",
        "SES Route53 Hosted Zone ID parameter",
    )

    identity = template["Resources"]["SesDomainIdentityResource"]
    require(
        identity["Properties"]["EmailIdentity"] == "SesDomainIdentity",
        "SES identity Ref",
    )
    require(
        identity["Properties"]["MailFromAttributes"]
        == {
            "BehaviorOnMxFailure": "REJECT_MESSAGE",
            "MailFromDomain": "SesMailFromDomain",
        },
        "SES MAIL FROM fail-closed contract",
    )
    require(
        identity["Properties"].get("DkimAttributes") == {"SigningEnabled": True},
        "SES DKIM signing",
    )
    require(
        identity["Properties"].get("ConfigurationSetAttributes")
        == {"ConfigurationSetName": "SesFeedbackConfigurationSet"}
        and identity.get("DependsOn") == [
            "SesFeedbackEventDestination", "SesSenderUser", "NonprodSesSenderUser"
        ],
        "SES identity default feedback configuration set",
    )
    resources = template["Resources"]
    require(
        resources["SesFeedbackConfigurationSet"]["Properties"].get("SuppressionOptions")
        == {"SuppressedReasons": ["BOUNCE", "COMPLAINT"]},
        "SES feedback suppression must cover hard bounces and complaints",
    )
    require(
        resources["SesFeedbackConfigurationSet"].get("DeletionPolicy") == "Retain"
        and resources["SesFeedbackConfigurationSet"].get("UpdateReplacePolicy") == "Retain",
        "SES feedback default must outlive the retained domain identity",
    )
    require(
        "Default" not in parameters["FeedbackNotificationEmailAddress"]
        and resources["SesFeedbackSubscription"]["Properties"]
        == {
            "TopicArn": "SesFeedbackTopic",
            "Protocol": "email",
            "Endpoint": "FeedbackNotificationEmailAddress",
        },
        "SES feedback requires an explicit operator email subscription",
    )
    require(
        resources["SesFeedbackEventDestination"].get("DependsOn")
        == "SesFeedbackTopicPolicy"
        and resources["SesFeedbackEventDestination"]["Properties"]
        == {
            "ConfigurationSetName": "SesFeedbackConfigurationSet",
            "EventDestination": {
                "Name": "bounce-complaint-email",
                "Enabled": True,
                "MatchingEventTypes": ["BOUNCE", "COMPLAINT"],
                "SnsDestination": {"TopicARN": "SesFeedbackTopic"},
            },
        },
        "SES feedback publishes only bounce and complaint events",
    )
    require(
        resources["SesFeedbackTopicPolicy"]["Properties"]
        == {
            "Topics": ["SesFeedbackTopic"],
            "PolicyDocument": {
                "Version": "2012-10-17",
                "Statement": [{
                    "Sid": "AllowSesFeedbackPublishing",
                    "Effect": "Allow",
                    "Principal": {"Service": "ses.amazonaws.com"},
                    "Action": "sns:Publish",
                    "Resource": "SesFeedbackTopic",
                    "Condition": {"StringEquals": {
                        "AWS:SourceAccount": "AWS::AccountId",
                        "AWS:SourceArn": "arn:${AWS::Partition}:ses:${AWS::Region}:${AWS::AccountId}:configuration-set/${SesFeedbackConfigurationSet}",
                    }},
                }],
            },
        },
        "SES feedback SNS publish permission must be account/configuration-set scoped",
    )
    for user, from_parameter in (
        ("SesSenderUser", "SenderEmailAddress"),
        ("NonprodSesSenderUser", "NonprodSenderEmailAddress"),
    ):
        statement = resources[user]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"][0]
        require(
            statement["Action"] == "ses:SendEmail"
            and statement["Resource"][:2] == [
                "arn:${AWS::Partition}:ses:${AWS::Region}:${AWS::AccountId}:identity/${SesDomainIdentity}",
                "arn:${AWS::Partition}:ses:${AWS::Region}:${AWS::AccountId}:configuration-set/${SesFeedbackConfigurationSet}",
            ]
            and statement["Condition"] == {"StringEquals": {"ses:FromAddress": from_parameter}},
            f"{user}: feedback access must retain exact sender restriction",
        )

    route53_records = {
        name: resource
        for name, resource in template["Resources"].items()
        if resource.get("Type") == "AWS::Route53::RecordSet"
    }
    require(
        set(route53_records)
        == {
            "DkimDnsRecord1",
            "DkimDnsRecord2",
            "DkimDnsRecord3",
            "MailFromMxDnsRecord",
            "MailFromSpfDnsRecord",
            "DmarcDnsRecord",
        },
        "SES Route53 record set",
    )
    # rua는 공개 DNS에 노출되므로 SNS 알림 주소가 아닌 별도 선택 파라미터만 쓴다.
    dmarc_parameter = parameters["DmarcReportEmailAddress"]
    require(
        dmarc_parameter.get("Type") == "String"
        and dmarc_parameter.get("Default") == ""
        and dmarc_parameter.get("MaxLength") == 254
        and dmarc_parameter.get("AllowedPattern")
        == "^$|^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}$",
        "SES DMARC report address parameter must be optional and empty by default",
    )
    require(
        template["Conditions"].get("HasDmarcReportAddress")
        == [["DmarcReportEmailAddress", ""]],
        "SES DMARC report address condition",
    )
    # 보고로 정렬 실패가 없음을 확인한 뒤 p를 올릴 수 있게 정책 값만 열어 둔다.
    dmarc_record = route53_records["DmarcDnsRecord"]
    require(
        dmarc_record.get("DeletionPolicy") == "Retain"
        and dmarc_record.get("UpdateReplacePolicy") == "Retain"
        and dmarc_record.get("Properties")
        in [
            {
                "HostedZoneId": "Route53HostedZoneId",
                "Name": "_dmarc.${SesDomainIdentity}",
                "Type": "TXT",
                "TTL": "300",
                "ResourceRecords": [[
                    "HasDmarcReportAddress",
                    f'"v=DMARC1; p={policy}; rua=mailto:${{DmarcReportEmailAddress}}"',
                    f'"v=DMARC1; p={policy}"',
                ]],
            }
            for policy in ("none", "quarantine", "reject")
        ],
        "SES DMARC TXT record must publish p=none|quarantine|reject, "
        "with rua only from DmarcReportEmailAddress",
    )
    for index in range(1, 4):
        record = route53_records[f"DkimDnsRecord{index}"]
        require(
            record.get("DeletionPolicy") == "Retain"
            and record.get("UpdateReplacePolicy") == "Retain"
            and record.get("Properties")
            == {
                "HostedZoneId": "Route53HostedZoneId",
                "Name": f"SesDomainIdentityResource.DkimDNSTokenName{index}",
                "Type": "CNAME",
                "TTL": "300",
                "ResourceRecords": [
                    f"SesDomainIdentityResource.DkimDNSTokenValue{index}"
                ],
            },
            f"SES DKIM CNAME {index}",
        )
    expected_mail_from_records = {
        "MailFromMxDnsRecord": {
            "HostedZoneId": "Route53HostedZoneId",
            "Name": "SesMailFromDomain",
            "Type": "MX",
            "TTL": "300",
            "ResourceRecords": [
                "10 feedback-smtp.${AWS::Region}.amazonses.com"
            ],
        },
        "MailFromSpfDnsRecord": {
            "HostedZoneId": "Route53HostedZoneId",
            "Name": "SesMailFromDomain",
            "Type": "TXT",
            "TTL": "300",
            "ResourceRecords": ['"v=spf1 include:amazonses.com ~all"'],
        },
    }
    for name, expected_properties in expected_mail_from_records.items():
        record = route53_records[name]
        require(
            record.get("DeletionPolicy") == "Retain"
            and record.get("UpdateReplacePolicy") == "Retain"
            and record.get("Properties") == expected_properties,
            f"SES MAIL FROM Route53 record: {name}",
        )

    outputs = template["Outputs"]
    require(
        outputs["MailFromDomain"]["Value"] == "SesMailFromDomain",
        "MAIL FROM output",
    )
    require(
        outputs["MailFromMxPriority"]["Value"] == "10"
        and outputs["MailFromMxValue"]["Value"]
        == "feedback-smtp.${AWS::Region}.amazonses.com",
        "MAIL FROM MX output",
    )
    require(
        outputs["MailFromSpfValue"]["Value"]
        == "v=spf1 include:amazonses.com ~all",
        "MAIL FROM SPF output",
    )


# 계정에서 스택 밖 수동 자원과 구분하고 용도를 찾을 수 있도록 IAM·S3 자원에 같은 태그를 붙인다.
def validate_cloudformation_tags() -> None:
    tagged_types = {"AWS::IAM::User", "AWS::IAM::Role", "AWS::S3::Bucket"}
    for template_path in sorted((ROOT / "cloud" / "aws").glob("*.yaml")):
        with template_path.open(encoding="utf-8") as stream:
            template = yaml.load(stream, Loader=CloudFormationLoader)
        for name, item in template.get("Resources", {}).items():
            if item.get("Type") not in tagged_types:
                continue
            tags = {
                tag.get("Key"): tag.get("Value")
                for tag in item.get("Properties", {}).get("Tags", [])
            }
            require(
                tags.get("managed-by") == "cloudformation"
                and bool(tags.get("environment"))
                and bool(tags.get("purpose")),
                f"{template_path.name}:{name} must carry managed-by=cloudformation, "
                "environment, and purpose tags",
            )


def resource(resources: list[dict], kind: str) -> dict:
    matches = [item for item in resources if item.get("kind") == kind]
    require(len(matches) == 1, f"expected one {kind}, found {len(matches)}")
    return matches[0]


class SecretReference(NamedTuple):
    # None이면 어떤 container도 마운트하지 않은 Pod volume이다.
    container: str | None
    secret: str
    # None이면 Secret의 모든 key를 받는다.
    keys: frozenset[str] | None


def secret_items(source: dict) -> frozenset[str] | None:
    items = source.get("items")
    return frozenset(item["key"] for item in items) if items else None


def volume_secrets(volume: dict) -> list[tuple[str, frozenset[str] | None]]:
    if "secret" in volume:
        return [(volume["secret"]["secretName"], secret_items(volume["secret"]))]
    return [
        (source["secret"]["name"], secret_items(source["secret"]))
        for source in volume.get("projected", {}).get("sources", [])
        if "secret" in source
    ]


def pod_secret_references(pod: dict) -> list[SecretReference]:
    containers = [*pod.get("initContainers", []), *pod["containers"]]
    references = []
    for volume in pod.get("volumes", []):
        readers = [
            container["name"]
            for container in containers
            if any(mount["name"] == volume["name"] for mount in container.get("volumeMounts", []))
        ] or [None]
        references += [
            SecretReference(reader, secret, keys)
            for reader in readers
            for secret, keys in volume_secrets(volume)
        ]
    for container in containers:
        for source in container.get("envFrom", []):
            if "secretRef" in source:
                references.append(
                    SecretReference(container["name"], source["secretRef"]["name"], None)
                )
        for item in container.get("env", []):
            reference = item.get("valueFrom", {}).get("secretKeyRef")
            if reference:
                references.append(SecretReference(
                    container["name"], reference["name"], frozenset({reference["key"]})
                ))
    return references


def preview_cert_manager_resources() -> list[dict]:
    return [
        item
        for path in sorted((ROOT / "manifests" / "cert-manager").glob("*.yaml"))
        for item in yaml_documents(path)
        if item.get("metadata", {}).get("namespace") == "preview"
    ]


# 공개 wildcard 키는 Traefik만 읽는다. PR Pod에는 loopback 이름만 담은 자체 서명 키를 Mailpit에만 준다.
def validate_preview_mail_capture_tls(
    resources: list[dict], cert_manager_resources: list[dict]
) -> None:
    pod = resource(resources, "Deployment")["spec"]["template"]["spec"]
    certificates = [item for item in cert_manager_resources if item["kind"] == "Certificate"]
    wildcard_secrets = {
        item["spec"]["secretName"]
        for item in certificates
        if any(name.startswith("*.") for name in item["spec"]["dnsNames"])
    }
    require(bool(wildcard_secrets), "preview: shared wildcard Certificate must exist")
    references = pod_secret_references(pod)
    leaked = {item.secret for item in references} & wildcard_secrets
    require(
        not leaked,
        f"preview: Deployment must not reference wildcard TLS Secret {sorted(leaked)}",
    )

    loopback = [
        item for item in certificates if item["spec"]["dnsNames"] == [MAIL_CAPTURE_SMTP_HOST]
    ]
    require(len(loopback) == 1, "preview: exactly one Mailpit loopback Certificate")
    spec = loopback[0]["spec"]
    issuer_ref = spec["issuerRef"]
    issuers = {
        item["metadata"]["name"]: item
        for item in cert_manager_resources
        if item["kind"] == "Issuer"
    }
    require(
        issuer_ref.get("group") == "cert-manager.io"
        and issuer_ref.get("kind") == "Issuer"
        and issuers.get(issuer_ref.get("name"), {}).get("spec") == {"selfSigned": {}},
        "preview: Mailpit Certificate must use the namespaced self-signed Issuer",
    )
    # 자체 서명 인증서의 issuer DN은 subject와 같고, Java는 빈 issuer DN을 거부한다.
    require(
        spec.get("commonName") == MAIL_CAPTURE_SMTP_HOST
        and spec.get("isCA") is False
        and spec["secretName"] == MAIL_CAPTURE_TLS_SECRET,
        "preview: Mailpit Certificate subject, CA flag and Secret",
    )

    loopback_references = [
        item for item in references if item.secret == MAIL_CAPTURE_TLS_SECRET
    ]
    require(
        [item.keys for item in loopback_references if item.container == "mail-capture-truststore"]
        == [frozenset({"tls.crt"})],
        "preview: truststore builder must receive only the public certificate",
    )
    key_readers = {
        item.container
        for item in loopback_references
        if item.keys is None or "tls.key" in item.keys
    }
    require(
        key_readers == {"mailpit"},
        "preview: only Mailpit may read the loopback private key",
    )
    builder = next(
        (item for item in pod.get("initContainers", []) if item["name"] == "mail-capture-truststore"),
        {},
    )
    app = pod["containers"][0]
    require(
        builder.get("image") == app["image"]
        and builder.get("securityContext", {}).get("readOnlyRootFilesystem") is True
        and builder["securityContext"].get("allowPrivilegeEscalation") is False
        and builder["securityContext"].get("capabilities") == {"drop": ["ALL"]},
        "preview: truststore builder must use the app image under the restricted profile",
    )
    app_env = {item["name"]: item.get("value") for item in app["env"]}
    require(
        app_env.get("JAVA_TOOL_OPTIONS") == MAIL_CAPTURE_JAVA_TOOL_OPTIONS,
        "preview: app must trust the Mailpit certificate through the built truststore",
    )


def edge_expectation(environment: str) -> tuple[str, str, str]:
    return {
        "prod": (
            "api.university.neordinary.com",
            "api-university-neordinary-com-tls",
            "umc-product-server",
        ),
        "dev": (
            "api-dev.university.neordinary.com",
            "api-dev-university-neordinary-com-tls",
            "umc-product-server",
        ),
        "preview": (
            "api-pr-42.university.neordinary.com",
            "preview-wildcard-tls",
            "umc-product-server-pr-42",
        ),
    }[environment]


def router_middlewares(environment: str, *names: str) -> str:
    _, _, service = edge_expectation(environment)
    return ",".join(
        f"{APP_NAMESPACES[environment]}-{service}-{name}@kubernetescrd" for name in names
    )


def require_positive_int(value: object, label: str) -> None:
    require(
        isinstance(value, int) and not isinstance(value, bool) and value > 0,
        f"{label} must be a positive integer (0 means unlimited in Traefik)",
    )


# 앱 host의 공격성 트래픽을 Traefik에서 먼저 거르는지 검사한다. 한도 값은 환경별로 바꿀 수 있어
# 1 이상의 정수인지만 보고, 기준이 요청 host나 위조 가능한 X-Forwarded-For로 바뀌지 않았는지 본다.
def validate_edge_protection(resources: list[dict], environment: str) -> None:
    expected_host, expected_tls_secret, expected_service = edge_expectation(environment)
    by_name = {
        item["metadata"].get("name"): item
        for item in resources
        if item.get("kind") in {"Middleware", "Ingress"}
    }

    def edge_middleware(suffix: str, kind: str, fields: set[str]) -> dict:
        name = f"{expected_service}-{suffix}"
        middleware = by_name.get(name, {})
        label = f"{environment}: edge Middleware {name}"
        require(
            middleware.get("kind") == "Middleware"
            and middleware["metadata"].get("annotations")
            == {"argocd.argoproj.io/sync-wave": "0"}
            and list(middleware.get("spec", {})) == [kind]
            and set(middleware["spec"][kind]) == fields | {"sourceCriterion"},
            f"{label} must exist at sync-wave 0 with only {kind} {sorted(fields)}",
        )
        require(
            middleware["spec"][kind]["sourceCriterion"] == {"ipStrategy": {"depth": 0}},
            f"{label} must count per client remote address (sourceCriterion.ipStrategy.depth: 0)",
        )
        return middleware["spec"][kind]

    rate_limit = edge_middleware("rate-limit", "rateLimit", {"average", "period", "burst"})
    require_positive_int(rate_limit["average"], f"{environment}: rateLimit.average")
    require_positive_int(rate_limit["burst"], f"{environment}: rateLimit.burst")
    require(
        isinstance(rate_limit["period"], str) and rate_limit["period"] != "",
        f"{environment}: rateLimit.period must be a duration string",
    )
    for suffix in ("in-flight", "ws-in-flight"):
        in_flight = edge_middleware(suffix, "inFlightReq", {"amount"})
        require_positive_int(in_flight["amount"], f"{environment}: {suffix} inFlightReq.amount")

    websocket = by_name.get(f"{expected_service}-ws", {})
    require(
        websocket.get("kind") == "Ingress"
        and websocket["metadata"].get("annotations")
        == {
            "argocd.argoproj.io/sync-wave": "1",
            "traefik.ingress.kubernetes.io/router.entrypoints": "websecure",
            "traefik.ingress.kubernetes.io/router.middlewares": router_middlewares(
                environment, "rate-limit", "ws-in-flight"
            ),
            "traefik.ingress.kubernetes.io/router.priority": "100",
        }
        and websocket.get("spec")
        == {
            "ingressClassName": "traefik",
            "rules": [
                {
                    "host": expected_host,
                    "http": {
                        "paths": [
                            {
                                "path": "/ws",
                                "pathType": "Prefix",
                                "backend": {
                                    "service": {
                                        "name": expected_service,
                                        "port": {"name": "http"},
                                    }
                                },
                            }
                        ]
                    },
                }
            ],
            "tls": [{"hosts": [expected_host], "secretName": expected_tls_secret}],
        },
        f"{environment}: WebSocket route must use rate-limit and the WebSocket in-flight limit",
    )
    edge_refs = router_middlewares(environment, "rate-limit", "in-flight").split(",")
    for ingress in resources:
        if ingress.get("kind") != "Ingress" or ingress["metadata"].get("name") == websocket["metadata"]["name"]:
            continue
        attached = ingress["metadata"].get("annotations", {}).get(
            "traefik.ingress.kubernetes.io/router.middlewares", ""
        ).split(",")
        require(
            attached[: len(edge_refs)] == edge_refs,
            f"{environment}: {ingress['metadata'].get('name')} must apply edge limits first",
        )


def require_public_ipv4(value: str, label: str) -> None:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise SystemExit(f"contract validation failed: {label}: {error}") from error
    require(address.version == 4 and address.is_global, f"{label}: public IPv4 required")


def validate_ingress_contract(
    ingress: dict,
    environment: str,
    expected_target: str,
    *,
    test_net_fixture: bool = False,
) -> None:
    expected_host, expected_secret, expected_service = edge_expectation(environment)
    if test_net_fixture:
        require(
            expected_target == TEST_NET_EDGE_TARGET,
            f"{environment}: synthetic edge fixture must use TEST-NET target",
        )
    else:
        require_public_ipv4(expected_target, f"{environment}: ExternalDNS source target")

    require(
        ingress["spec"].get("ingressClassName") == "traefik",
        f"{environment}: Traefik Ingress class",
    )
    require(
        ingress["spec"].get("rules")
        == [
            {
                "host": expected_host,
                "http": {
                    "paths": [
                        {
                            "path": "/",
                            "pathType": "Prefix",
                            "backend": {
                                "service": {
                                    "name": expected_service,
                                    "port": {"name": "http"},
                                }
                            },
                        }
                    ]
                },
            }
        ],
        f"{environment}: exact public route",
    )
    require(
        ingress["spec"].get("tls")
        == [{"hosts": [expected_host], "secretName": expected_secret}],
        f"{environment}: TLS binding",
    )
    require(
        ingress["metadata"].get("annotations")
        == {
            "argocd.argoproj.io/sync-wave": "1",
            "external-dns.kubernetes.io/managed-by": "umc-infra",
            "external-dns.kubernetes.io/target": expected_target,
            "traefik.ingress.kubernetes.io/router.entrypoints": "websecure",
            "traefik.ingress.kubernetes.io/router.middlewares": router_middlewares(
                environment, "rate-limit", "in-flight"
            ),
        },
        f"{environment}: exact Route53-backed Ingress annotations",
    )


# 문서 경로에만 BasicAuth를 적용하고 일반 API의 인증 흐름을 바꾸지 않는지 검사한다.
def validate_documentation_ingress_contract(
    ingress: dict,
    middleware: dict,
    environment: str,
) -> None:
    expected_host, expected_tls_secret, expected_service = edge_expectation(environment)
    middleware_name = f"{expected_service}-docs-basic-auth"

    require(
        middleware["metadata"].get("name") == middleware_name
        and middleware["metadata"].get("annotations")
        == {"argocd.argoproj.io/sync-wave": "0"}
        and middleware.get("spec")
        == {
            "basicAuth": {
                "secret": "docs-basic-auth",
                "removeHeader": True,
            }
        },
        f"{environment}: docs Basic Auth Middleware",
    )
    require(
        ingress["metadata"].get("name") == f"{expected_service}-docs"
        and ingress["metadata"].get("annotations")
        == {
            "argocd.argoproj.io/sync-wave": "1",
            "traefik.ingress.kubernetes.io/router.entrypoints": "websecure",
            "traefik.ingress.kubernetes.io/router.middlewares": router_middlewares(
                environment, "rate-limit", "in-flight", "docs-basic-auth"
            ),
            "traefik.ingress.kubernetes.io/router.priority": "100",
        },
        f"{environment}: docs Ingress annotations",
    )
    expected_paths = []
    for path in ("/docs", "/docs-json"):
        expected_paths.append(
            {
                "path": path,
                "pathType": "Prefix",
                "backend": {
                    "service": {
                        "name": expected_service,
                        "port": {"name": "http"},
                    }
                },
            }
        )
    require(
        ingress["spec"]
        == {
            "ingressClassName": "traefik",
            "rules": [
                {
                    "host": expected_host,
                    "http": {"paths": expected_paths},
                }
            ],
            "tls": [
                {"hosts": [expected_host], "secretName": expected_tls_secret}
            ],
        },
        f"{environment}: docs routes and TLS",
    )


def validate_test_api_ingress_contract(
    ingress: dict,
    middleware: dict,
) -> None:
    expected_host, expected_tls_secret, expected_service = edge_expectation("dev")
    middleware_name = f"{expected_service}-test-api-basic-auth"

    require(
        middleware["metadata"].get("name") == middleware_name
        and middleware["metadata"].get("annotations")
        == {"argocd.argoproj.io/sync-wave": "0"}
        and middleware.get("spec")
        == {
            "basicAuth": {
                "secret": "docs-basic-auth",
                "removeHeader": True,
            }
        },
        "dev: test API Basic Auth Middleware",
    )
    require(
        ingress["metadata"].get("name") == f"{expected_service}-test-api"
        and ingress["metadata"].get("annotations")
        == {
            "argocd.argoproj.io/sync-wave": "1",
            "traefik.ingress.kubernetes.io/router.entrypoints": "websecure",
            "traefik.ingress.kubernetes.io/router.middlewares": router_middlewares(
                "dev", "rate-limit", "in-flight", "test-api-basic-auth"
            ),
            "traefik.ingress.kubernetes.io/router.priority": "100",
        },
        "dev: test API Ingress annotations",
    )
    require(
        ingress["spec"]
        == {
            "ingressClassName": "traefik",
            "rules": [
                {
                    "host": expected_host,
                    "http": {
                        "paths": [
                            {
                                "path": "/test",
                                "pathType": "Prefix",
                                "backend": {
                                    "service": {
                                        "name": expected_service,
                                        "port": {"name": "http"},
                                    }
                                },
                            }
                        ]
                    },
                }
            ],
            "tls": [
                {"hosts": [expected_host], "secretName": expected_tls_secret}
            ],
        },
        "dev: protected test API route and TLS",
    )


# 환경별 렌더 결과에서 이미지·Secret 주입·네트워크·배포 gate가 함께 유지되는지 확인한다.
def validate_render(
    path: Path,
    environment: str,
    ingress_enabled: bool,
    test_api_enabled: bool,
) -> None:
    resources = yaml_documents(path)
    deployment = resource(resources, "Deployment")
    service = resource(resources, "Service")
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]

    require(deployment["spec"]["replicas"] == 1, f"{environment}: replicas must be 1")
    require(deployment["spec"]["strategy"] == {"type": "Recreate"}, f"{environment}: strategy")
    require(pod["terminationGracePeriodSeconds"] == 60, f"{environment}: termination grace")
    expected_image = PREVIEW_APP_IMAGE if environment == "preview" else APP_IMAGE
    require(container["image"] == expected_image, f"{environment}: application image contract")
    require("imagePullSecrets" not in pod, f"{environment}: public GHCR must use anonymous pull")
    require(
        {port["name"]: port["containerPort"] for port in container["ports"]}
        == {"http": 8080, "management": 9090},
        f"{environment}: container ports",
    )
    # Preview는 Pod 안 Mailpit UI만 추가로 연다. SMTP와 management는 Service로 노출하지 않는다.
    expected_service_ports = [
        {"name": "http", "port": 80, "targetPort": "http", "protocol": "TCP"}
    ] + (
        [{"name": "mail-ui", "port": 8025, "targetPort": "mail-ui", "protocol": "TCP"}]
        if environment == "preview"
        else []
    )
    require(
        service["spec"]["type"] == "ClusterIP"
        and service["spec"]["ports"] == expected_service_ports,
        f"{environment}: management port must not be exposed",
    )
    probes = {
        "startupProbe": "/actuator/health/liveness",
        "livenessProbe": "/actuator/health/liveness",
        "readinessProbe": "/actuator/health/readiness",
    }
    for probe_name, expected_path in probes.items():
        probe = container[probe_name]["httpGet"]
        require(
            probe == {"path": expected_path, "port": "management"},
            f"{environment}: {probe_name}",
        )
    require(
        container["lifecycle"]["preStop"]["exec"]["command"]
        == ["sh", "-c", "sleep 5"],
        f"{environment}: preStop",
    )
    require(pod["securityContext"]["runAsNonRoot"] is True, f"{environment}: pod non-root")
    require(
        container["securityContext"]["readOnlyRootFilesystem"] is True,
        f"{environment}: read-only root filesystem",
    )
    expected_required_secrets = (
        NONPREVIEW_REQUIRED_SECRETS if environment in {"prod", "dev"} else BASE_REQUIRED_SECRETS
    )
    require(
        [item["secretRef"]["name"] for item in container["envFrom"]]
        == expected_required_secrets,
        f"{environment}: required Secret order",
    )
    expected_reloader_secrets = (
        NONPREVIEW_RELOADER_SECRETS
        if environment in {"prod", "dev"}
        else [*BASE_RELOADER_SECRETS, MAIL_CAPTURE_TLS_SECRET]
    )
    actual_reloader_secrets = deployment["metadata"].get("annotations", {}).get(
        RELOADER_TRIGGER_ANNOTATION
    )
    require(
        actual_reloader_secrets == ",".join(expected_reloader_secrets),
        f"{environment}: named Reloader Secret allowlist",
    )
    require(
        not {"app-db", "app-jwt"} & set(actual_reloader_secrets.split(",")),
        f"{environment}: coordinated Secrets must not trigger automatic reload",
    )
    require(
        RELOADER_TRIGGER_ANNOTATION
        not in deployment["spec"]["template"]["metadata"].get("annotations", {}),
        f"{environment}: Reloader trigger must be a workload annotation",
    )
    environment_values = {item["name"]: item.get("value") for item in container["env"]}
    common = {
        "MANAGEMENT_ENDPOINT_HEALTH_PROBES_ENABLED": "true",
        "MANAGEMENT_SERVER_PORT": "9090",
        "OTEL_URL": "http://otel-collector.monitoring.svc.cluster.local:4318",
        "TRACE_SAMPLING_PROBABILITY": "1.0",
        "DEMODAY_QR_BASE_URL": "https://validation.example.com",
        "S3_REGION": "ap-northeast-2",
        "SES_REGION": "ap-northeast-2",
    }
    for key, expected in common.items():
        require(environment_values.get(key) == expected, f"{environment}: env {key}")

    expected_environment = {
        "prod": {
            "APP_SEED_ENABLED": "false",
            "APP_TEST_API_ENABLED": "false",
            "FCM_ENABLED": "true",
            "OPENAPI_ENABLE": "true",
            "SPRING_PROFILES_ACTIVE": "prod",
            "SPRING_APPLICATION_NAME": "prod-umc-product",
            "APP_ENVIRONMENT": "prod",
            "SSO_ISSUER": "https://api.university.neordinary.com",
            "SSO_SOCIAL_GOOGLE_CLIENT_ID": GOOGLE_CLIENT_ID_LIST.split(",")[0],
            "SSO_SOCIAL_GOOGLE_CALLBACK_URI": "https://api.university.neordinary.com/api/v1/auth/sso/social/google/callback",
            "SSO_SOCIAL_APPLE_CALLBACK_URI": "https://api.university.neordinary.com/api/v1/auth/sso/social/apple/callback",
            "SSO_SOCIAL_APPLE_ENABLED": "true",
            "SSO_SOCIAL_KAKAO_CALLBACK_URI": "https://api.university.neordinary.com/api/v1/auth/sso/social/kakao/callback",
            "SSO_SOCIAL_KAKAO_ENABLED": "true",
            "SSO_SOCIAL_KAKAO_OIDC": "true",
            "KAKAO_ALLOWED_REDIRECT_URIS": "https://api.university.neordinary.com/api/v1/auth/sso/social/kakao/callback",
            "KAKAO_ALLOWED_ID_TOKEN_AUDIENCES": KAKAO_JAVASCRIPT_KEY,
            "CERTIFICATE_VERIFICATION_URL_TEMPLATE": "https://api.university.neordinary.com/api/v1/certificates/verify/{serialNumber}",
            "S3_BUCKET_NAME": "umc-product-prod-app-storage-351284652562-ap-northeast-2",
            "GOOGLE_CLIENT_ID_LIST": GOOGLE_CLIENT_ID_LIST,
            "DATABASE_USERNAME": "umc_product_app",
            "DATABASE_URL": "jdbc:postgresql://postgres.db.svc.cluster.local:5432/umc_product",
        },
        "dev": {
            "APP_SEED_ENABLED": "true",
            "APP_TEST_API_ENABLED": "true",
            "FCM_ENABLED": "true",
            "OPENAPI_ENABLE": "true",
            "SPRING_PROFILES_ACTIVE": "dev",
            "SPRING_APPLICATION_NAME": "dev-umc-product",
            "APP_ENVIRONMENT": "dev",
            "SSO_ISSUER": "https://api-dev.university.neordinary.com",
            "SSO_SOCIAL_GOOGLE_CLIENT_ID": GOOGLE_CLIENT_ID_LIST.split(",")[0],
            "SSO_SOCIAL_GOOGLE_CALLBACK_URI": "https://api-dev.university.neordinary.com/api/v1/auth/sso/social/google/callback",
            "SSO_SOCIAL_APPLE_CALLBACK_URI": "https://api-dev.university.neordinary.com/api/v1/auth/sso/social/apple/callback",
            "SSO_SOCIAL_APPLE_ENABLED": "true",
            "SSO_SOCIAL_KAKAO_CALLBACK_URI": "https://api-dev.university.neordinary.com/api/v1/auth/sso/social/kakao/callback",
            "SSO_SOCIAL_KAKAO_ENABLED": "true",
            "SSO_SOCIAL_KAKAO_OIDC": "true",
            "KAKAO_ALLOWED_REDIRECT_URIS": "https://api-dev.university.neordinary.com/api/v1/auth/sso/social/kakao/callback",
            "KAKAO_ALLOWED_ID_TOKEN_AUDIENCES": KAKAO_JAVASCRIPT_KEY,
            "CERTIFICATE_VERIFICATION_URL_TEMPLATE": "https://api-dev.university.neordinary.com/api/v1/certificates/verify/{serialNumber}",
            "S3_BUCKET_NAME": "umc-product-dev-app-storage-351284652562-ap-northeast-2",
            "GOOGLE_CLIENT_ID_LIST": GOOGLE_CLIENT_ID_LIST,
            "DATABASE_USERNAME": "umc_product_dev_app",
            "DATABASE_URL": "jdbc:postgresql://postgres.dev-db.svc.cluster.local:5432/umc_product_dev",
        },
        "preview": {
            "APP_SEED_ENABLED": "true",
            "APP_TEST_API_ENABLED": "true",
            "FCM_ENABLED": "false",
            "SPRING_PROFILES_ACTIVE": "dev",
            "SPRING_APPLICATION_NAME": "preview-umc-product-pr42",
            "APP_ENVIRONMENT": "preview-pr-42",
            "SSO_ISSUER": "https://api-pr-42.university.neordinary.com",
            "CERTIFICATE_VERIFICATION_URL_TEMPLATE": "https://api-pr-42.university.neordinary.com/api/v1/certificates/verify/{serialNumber}",
            "S3_BUCKET_NAME": "umc-product-preview-app-storage-351284652562-ap-northeast-2",
            "DATABASE_USERNAME": "umc_product_preview_app",
            "DATABASE_URL": "jdbc:postgresql://postgres-preview.preview.svc.cluster.local:5432/umc_product_pr42",
        },
    }[environment]
    for key, expected in expected_environment.items():
        require(environment_values.get(key) == expected, f"{environment}: env {key}")

    require(
        "SSO_SOCIAL_GOOGLE_CLIENT_SECRET" not in environment_values,
        "Google Web client secret must come from Secret",
    )
    if environment in {"prod", "dev"}:
        require(
            environment_values.get("SSO_SOCIAL_GOOGLE_ENABLED") in {"true", "false"},
            f"{environment}: Google SSO enabled flag",
        )
    else:
        require(
            environment_values.get("SSO_SOCIAL_GOOGLE_ENABLED", "false") == "false",
            "preview must not enable Google server SSO",
        )

    provider = environment_values.get("EMAIL_PROVIDER")
    require(provider in {"ses", "smtp"}, f"{environment}: email provider")
    require("SMTP_PASSWORD" not in environment_values, "SMTP password must come from Secret")
    if provider == "smtp" and environment == "preview":
        # Preview는 Pod 안 Mailpit으로만 보낸다. 외부 SMTP 주소나 Gmail 계정을 쓰지 않는다.
        for key, expected in {
            "SMTP_HOST": "smtp-capture.university.neordinary.com", "SMTP_PORT": "1025",
            "SMTP_USERNAME": "umc-preview",
            "EMAIL_NO_REPLY_ADDRESS": "no-reply-nonprod@university.neordinary.com",
        }.items():
            require(environment_values.get(key) == expected, f"{environment}: env {key}")
    elif provider == "smtp":
        require(environment in {"prod", "dev"}, "preview must not use Gmail SMTP")
        for key, expected in {
            "SMTP_HOST": "smtp.gmail.com", "SMTP_PORT": "587",
            "SMTP_USERNAME": "umcproduct1227@gmail.com",
            "EMAIL_NO_REPLY_ADDRESS": "umcproduct1227@gmail.com",
        }.items():
            require(environment_values.get(key) == expected, f"{environment}: env {key}")
    else:
        expected_sender = ("no-reply@university.neordinary.com" if environment == "prod"
                           else "no-reply-nonprod@university.neordinary.com")
        require(environment_values.get("EMAIL_NO_REPLY_ADDRESS") == expected_sender,
                f"{environment}: SES sender")

    expected_host, expected_secret, expected_service = edge_expectation(environment)
    certificates = [item for item in resources if item.get("kind") == "Certificate"]
    certificate_issuer: str | None = None
    if environment == "preview":
        require(not certificates, "preview: per-PR Certificate must be disabled")
        validate_preview_mail_capture_tls(resources, preview_cert_manager_resources())
        require(
            service["metadata"].get("annotations", {}).get(
                "argocd.argoproj.io/sync-wave"
            )
            == "-2",
            "preview: Service quota gate must run before createdb",
        )
    else:
        certificate = resource(resources, "Certificate")
        require(
            certificate["metadata"].get("annotations", {}).get(
                "argocd.argoproj.io/sync-wave"
            )
            == "-2",
            f"{environment}: Certificate must precede database/application resources",
        )
        require(
            certificate["spec"]["dnsNames"] == [expected_host],
            f"{environment}: TLS host",
        )
        require(
            certificate["spec"]["secretName"] == expected_secret,
            f"{environment}: TLS Secret",
        )
        issuer_ref = certificate["spec"]["issuerRef"]
        certificate_issuer = issuer_ref.get("name")
        require(
            issuer_ref
            == {
                "group": "cert-manager.io",
                "kind": "ClusterIssuer",
                "name": certificate_issuer,
            }
            and certificate_issuer in CERTIFICATE_ISSUERS,
            f"{environment}: supported ClusterIssuer",
        )
        require(
            certificate["spec"]["privateKey"]
            == {"algorithm": "ECDSA", "size": 256, "rotationPolicy": "Always"},
            f"{environment}: certificate key contract",
        )

    documentation_enabled = environment in {"prod", "dev", "preview"}
    ingresses = [item for item in resources if item.get("kind") == "Ingress"]
    expected_ingress_count = (
        (2 + int(documentation_enabled) + int(test_api_enabled))
        if ingress_enabled
        else 0
    )
    require(
        len(ingresses) == expected_ingress_count,
        f"{environment}: rendered Ingress must match source gate",
    )
    middlewares = [item for item in resources if item.get("kind") == "Middleware"]
    require(
        len(middlewares)
        == (
            3 + int(documentation_enabled) + int(test_api_enabled)
            if ingress_enabled
            else 0
        ),
        f"{environment}: edge and protected-route Middleware must match source gates",
    )
    if ingress_enabled:
        if environment != "preview":
            require(
                certificate_issuer == "letsencrypt-production",
                f"{environment}: active Ingress requires production Certificate",
            )
        source_values = yaml.safe_load(
            (ROOT / "charts" / "umc-product-server" / "values.yaml").read_text(
                encoding="utf-8"
            )
        )
        source_target = str(source_values["externalDNS"]["target"])
        main_ingress = next(
            item
            for item in ingresses
            if "external-dns.kubernetes.io/target"
            in item["metadata"].get("annotations", {})
        )
        validate_ingress_contract(main_ingress, environment, source_target)
        validate_edge_protection(resources, environment)
        if documentation_enabled:
            docs_ingress = next(
                item
                for item in ingresses
                if item["metadata"].get("name") == f"{expected_service}-docs"
            )
            docs_middleware = next(
                item
                for item in middlewares
                if item["metadata"].get("name")
                == f"{expected_service}-docs-basic-auth"
            )
            validate_documentation_ingress_contract(
                docs_ingress,
                docs_middleware,
                environment,
            )
        if test_api_enabled:
            test_api_ingress = next(
                item
                for item in ingresses
                if item["metadata"].get("name") == f"{expected_service}-test-api"
            )
            test_api_middleware = next(
                item
                for item in middlewares
                if item["metadata"].get("name")
                == f"{expected_service}-test-api-basic-auth"
            )
            validate_test_api_ingress_contract(
                test_api_ingress,
                test_api_middleware,
            )

    policies = [item for item in resources if item.get("kind") == "NetworkPolicy"]
    require(len(policies) == (0 if environment == "preview" else 2), f"{environment}: policies")
    if environment == "preview":
        validate_preview_budget_arithmetic(resources)
        jobs = [item for item in resources if item.get("kind") == "Job"]
        require(len(jobs) == 2, "preview: createdb and dropdb Jobs")
        dropdb = next(
            item
            for item in jobs
            if item["metadata"]["labels"]["app.kubernetes.io/component"]
            == "database-cleanup"
        )
        require(
            dropdb["metadata"]["annotations"]["argocd.argoproj.io/hook"] == "PostDelete",
            "preview: dropdb must run after workload deletion",
        )
        dropdb_script = dropdb["spec"]["template"]["spec"]["containers"][0]["args"][0]
        require(
            'DROP DATABASE IF EXISTS :"db" WITH (FORCE);' in dropdb_script,
            "preview: dropdb must force-disconnect only the quoted target database",
        )
        createdb = next(
            item
            for item in jobs
            if item["metadata"]["labels"]["app.kubernetes.io/component"]
            == "database-bootstrap"
        )
        createdb_container = createdb["spec"]["template"]["spec"]["containers"][0]
        require(
            createdb["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] == "-1",
            "preview: Service quota gate must run before createdb",
        )
        require(createdb_container["image"] == POSTGIS_IMAGE, "preview: PostGIS client")
        script = createdb_container["args"][0]
        require("CREATE EXTENSION IF NOT EXISTS postgis;" in script, "preview: postgis bootstrap")
        require("CREATE EXTENSION IF NOT EXISTS btree_gist;" in script, "preview: btree_gist bootstrap")
        createdb_env = {item["name"]: item for item in createdb_container["env"]}
        require(
            createdb_env["COPY_FROM_DEV"]["value"] == "true"
            and createdb_env["SOURCE_USER"]["value"] == "umc_product_dev_preview_dump"
            and createdb_env["SOURCE_PASSWORD"]["valueFrom"]["secretKeyRef"]["name"] == "dev-db-dump",
            "preview: createdb must copy dev only through the read-only dump role",
        )
        require(
            createdb["spec"]["template"]["metadata"]["labels"].get(
                "infra.university.neordinary.com/dev-db-reader"
            )
            == "true",
            "preview: only the createdb Pod may match the dev DB reader NetworkPolicy",
        )
        require(
            "WITH (FORCE)" in script and "umc-preview-restore-complete" in script,
            "preview: createdb must rebuild only databases without the restore marker",
        )
        # 공용 DB 대기 상한(600초)이 Job deadline 안에 들어가야 대기 중 Job이 죽지 않는다.
        # 시도 횟수로 세면 pg_isready timeout만큼 상한이 늘어나므로 경과 시간으로 끊는지도 본다.
        require(
            'wait_for "$$PGHOST" 600' in script
            and "pg_isready -q -t 1" in script
            and "while [ \"$$SECONDS\" -lt \"$$deadline\" ]" in script
            and createdb["spec"]["activeDeadlineSeconds"] >= 900,
            "preview: createdb must wait 10 minutes for the shared DB within its deadline",
        )


def validate_active_edge_fixture(path: Path) -> None:
    resources = yaml_documents(path)
    ingress = next(
        item
        for item in resources
        if item.get("kind") == "Ingress"
        and "external-dns.kubernetes.io/target"
        in item["metadata"].get("annotations", {})
    )
    certificate = resource(resources, "Certificate")
    require(
        certificate["spec"].get("issuerRef")
        == {
            "group": "cert-manager.io",
            "kind": "ClusterIssuer",
            "name": "letsencrypt-production",
        },
        "active edge fixture: production Certificate",
    )
    require(
        certificate["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] == "-2",
        "active edge fixture: Certificate wave",
    )
    validate_ingress_contract(
        ingress,
        "prod",
        TEST_NET_EDGE_TARGET,
        test_net_fixture=True,
    )


def validate_active_preview_edge_fixture(path: Path) -> None:
    resources = yaml_documents(path)
    ingress = next(
        item
        for item in resources
        if item.get("kind") == "Ingress"
        and item["metadata"]["name"] == "umc-product-server-pr-42"
    )
    mail_ingress = next(
        item
        for item in resources
        if item.get("kind") == "Ingress"
        and item["metadata"]["name"] == "umc-product-server-pr-42-mail"
    )
    require(
        mail_ingress["spec"]["rules"][0]["host"] == "mail-pr-42.university.neordinary.com"
        and mail_ingress["spec"]["tls"][0]["secretName"] == "preview-wildcard-tls"
        and mail_ingress["metadata"]["annotations"]["traefik.ingress.kubernetes.io/router.middlewares"]
        == router_middlewares("preview", "rate-limit", "in-flight", "mail-basic-auth"),
        "active preview edge fixture: mail capture UI must use the wildcard TLS and Basic Auth",
    )
    require(
        not any(item.get("kind") == "Certificate" for item in resources),
        "active preview edge fixture: per-PR Certificate must stay disabled",
    )
    validate_ingress_contract(
        ingress,
        "preview",
        TEST_NET_EDGE_TARGET,
        test_net_fixture=True,
    )
    validate_edge_protection(resources, "preview")


# DB 환경 분리와 역할·백업·저장 경로를 고정해 chart나 초기화 Job 변경의 데이터 위험을 검사한다.
def validate_postgres() -> None:
    expected_databases = {
        "prod": "umc_product",
        "dev": "umc_product_dev",
        "preview": "preview_bootstrap",
    }
    for environment, expected_database in expected_databases.items():
        directory = ROOT / "manifests" / "postgres" / environment
        statefulset = resource(yaml_documents(directory / "statefulset.yaml"), "StatefulSet")
        container = statefulset["spec"]["template"]["spec"]["containers"][0]
        require(container["image"] == POSTGIS_IMAGE, f"{environment}: PostGIS image")
        mounts = {item["name"]: item["mountPath"] for item in container["volumeMounts"]}
        require(mounts["data"] == "/var/lib/postgresql", f"{environment}: PG18 volume path")
        environment_values = {
            item["name"]: item.get("value") for item in container.get("env", [])
        }
        require("PGDATA" not in environment_values, f"{environment}: use image PG18 PGDATA")
        require(
            environment_values["POSTGRES_DB"] == expected_database,
            f"{environment}: database name",
        )
        # 새 데이터 디렉터리는 한글 정렬을 위해 ICU ko-KR로 만든다. libc locale도 image LANG에 기대지 않는다.
        require(
            environment_values.get("POSTGRES_INITDB_ARGS")
            == "--encoding=UTF8 --locale=C.UTF-8 --locale-provider=icu --icu-locale=ko-KR",
            f"{environment}: initdb must use the ICU ko-KR default collation",
        )
        require(
            statefulset["spec"].get("persistentVolumeClaimRetentionPolicy")
            == {"whenDeleted": "Retain", "whenScaled": "Retain"}
            and statefulset["spec"].get("updateStrategy") == {"type": "RollingUpdate"},
            f"{environment}: StatefulSet must retain data PVCs and roll Pods one at a time",
        )
        if environment == "prod":
            # CPU limit은 checkpoint·vacuum을 throttle한다. memory는 request와 limit을 같게 둔다.
            resources = container["resources"]
            require(
                "cpu" not in resources.get("limits", {})
                and resources["requests"].get("cpu")
                and resources["requests"]["memory"] == resources["limits"]["memory"],
                "prod: PostgreSQL keeps a CPU request without a CPU limit and fixed memory",
            )
        if environment == "preview":
            require(
                "max_connections=40" in container.get("args", []),
                "preview: connection budget must match two slots",
            )
            orphan_check = resource(
                yaml_documents(directory / "orphan-database-check-cronjob.yaml"), "CronJob"
            )["spec"]
            require(
                orphan_check["schedule"] == "0 3 * * *"
                and orphan_check["timeZone"] == "Asia/Seoul",
                "preview: orphan DB check runs daily at 03:00 Asia/Seoul",
            )

        role_job = resource(yaml_documents(directory / "app-role-job.yaml"), "Job")
        role_container = role_job["spec"]["template"]["spec"]["containers"][0]
        require(role_container["image"] == POSTGIS_IMAGE, f"{environment}: role Job image")
        app_password = next(item for item in role_container["env"] if item["name"] == "APP_PASSWORD")
        require(
            app_password["valueFrom"]["secretKeyRef"]
            == {"name": "app-db", "key": "DATABASE_PASSWORD"},
            f"{environment}: app DB Secret",
        )
        if environment in {"prod", "dev"}:
            script = role_container["args"][0]
            require("CREATE EXTENSION IF NOT EXISTS postgis;" in script, f"{environment}: postgis")
            require("CREATE EXTENSION IF NOT EXISTS btree_gist;" in script, f"{environment}: btree_gist")
            # 앱 role이 DB owner이면 DROP DATABASE가 가능하다. owner는 관리자에게 두고 필요한 권한만 준다.
            app_role = "umc_product_app" if environment == "prod" else "umc_product_dev_app"
            require(
                'ALTER DATABASE :"DBNAME" OWNER TO CURRENT_USER;' in script
                and f'ALTER DATABASE :"DBNAME" OWNER TO {app_role}' not in script,
                f"{environment}: database owner must stay the admin role",
            )
            require(
                f'GRANT CONNECT, TEMPORARY, CREATE ON DATABASE :"DBNAME" TO {app_role};' in script,
                f"{environment}: app role needs CREATE for the Flyway public schema bootstrap",
            )

    prod_directory = ROOT / "manifests" / "postgres" / "prod"
    readonly_script = resource(
        yaml_documents(prod_directory / "readonly-role-job.yaml"), "Job"
    )["spec"]["template"]["spec"]["containers"][0]["args"][0]
    for statement in (
        "CONNECTION LIMIT 10;",
        "ALTER ROLE umc_product_ro SET statement_timeout = '60s';",
        "ALTER ROLE umc_product_ro SET idle_in_transaction_session_timeout = '5min';",
    ):
        require(statement in readonly_script, f"prod: readonly role limit {statement}")
    backup_dump = resource(
        yaml_documents(prod_directory / "backup-cronjob.yaml"), "CronJob"
    )["spec"]["jobTemplate"]["spec"]["template"]["spec"]["initContainers"][0]
    require(
        "pg_dump -Fc --lock-wait-timeout=5min " in backup_dump["args"][0],
        "prod: backup pg_dump must fail instead of waiting forever for table locks",
    )
    exporter_role_job = resource(
        yaml_documents(prod_directory / "postgres-exporter-role-job.yaml"), "Job"
    )
    exporter_role_container = exporter_role_job["spec"]["template"]["spec"][
        "containers"
    ][0]
    exporter_role_script = exporter_role_container["args"][0]
    require(
        exporter_role_container["image"] == POSTGIS_IMAGE,
        "prod: exporter role Job image",
    )
    require(
        "GRANT pg_monitor TO umc_product_exporter;" in exporter_role_script
        and "NOBYPASSRLS" in exporter_role_script
        and "GRANT SELECT" not in exporter_role_script,
        "prod: exporter role must be monitoring-only",
    )
    exporter_password = next(
        item
        for item in exporter_role_container["env"]
        if item["name"] == "EXPORTER_PASSWORD"
    )
    require(
        exporter_password["valueFrom"]["secretKeyRef"]
        == {
            "name": "postgres-exporter",
            "key": "POSTGRES_EXPORTER_PASSWORD",
        },
        "prod: exporter role Secret",
    )

    exporter_resources = yaml_documents(prod_directory / "postgres-exporter.yaml")
    exporter_config = resource(exporter_resources, "ConfigMap")
    exporter_service_account = resource(exporter_resources, "ServiceAccount")
    exporter_service = resource(exporter_resources, "Service")
    exporter_deployment = resource(exporter_resources, "Deployment")
    exporter_pod = exporter_deployment["spec"]["template"]["spec"]
    exporter_container = exporter_pod["containers"][0]
    exporter_env = {item["name"]: item["value"] for item in exporter_container["env"]}
    require(
        exporter_config.get("data")
        == {"postgres_exporter.yml": "auth_modules: {}\n"},
        "prod: exporter config contains no credentials",
    )
    require(
        exporter_service_account.get("automountServiceAccountToken") is False
        and exporter_pod.get("automountServiceAccountToken") is False,
        "prod: exporter Kubernetes API token disabled",
    )
    require(
        exporter_container["image"] == POSTGRES_EXPORTER_IMAGE,
        "prod: postgres-exporter image",
    )
    require(
        exporter_env
        == {
            "DATA_SOURCE_URI": (
                "postgres.db.svc.cluster.local:5432/umc_product?sslmode=disable"
            ),
            "DATA_SOURCE_USER": "umc_product_exporter",
            "DATA_SOURCE_PASS_FILE": (
                "/var/run/secrets/postgres-exporter/POSTGRES_EXPORTER_PASSWORD"
            ),
            "PG_EXPORTER_COLLECTION_TIMEOUT": "8s",
        },
        "prod: exporter connection contract",
    )
    require(
        exporter_service["spec"]
        == {
            "type": "ClusterIP",
            "selector": {
                "app.kubernetes.io/name": "postgres-exporter",
                "app.kubernetes.io/component": "metrics",
            },
            "ports": [
                {
                    "name": "metrics",
                    "port": 9187,
                    "targetPort": "metrics",
                    "protocol": "TCP",
                }
            ],
        },
        "prod: exporter internal-only Service",
    )
    require(
        exporter_container["resources"]
        == {
            "requests": {"cpu": "10m", "memory": "32Mi"},
            "limits": {"memory": "64Mi"},
        },
        "prod: exporter resource budget",
    )
    require(
        exporter_pod["securityContext"].get("runAsNonRoot") is True
        and exporter_container["securityContext"]
        == {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        },
        "prod: exporter restricted security context",
    )

    policies = {
        item["metadata"]["name"]: item
        for item in yaml_documents(prod_directory / "networkpolicy.yaml")
        if item.get("kind") == "NetworkPolicy"
    }
    require(
        policies["postgres-exporter-allow-db-egress"]["spec"]
        == {
            "podSelector": {
                "matchLabels": {
                    "app.kubernetes.io/name": "postgres-exporter",
                    "app.kubernetes.io/component": "metrics",
                }
            },
            "policyTypes": ["Egress"],
            "egress": [
                {
                    "to": [
                        {
                            "podSelector": {
                                "matchLabels": {
                                    "app.kubernetes.io/name": "postgres",
                                    "app.kubernetes.io/component": "database",
                                }
                            }
                        }
                    ],
                    "ports": [{"protocol": "TCP", "port": 5432}],
                }
            ],
        },
        "prod: exporter DB-only egress",
    )
    prometheus_ingress = policies["postgres-exporter-allow-prometheus-ingress"][
        "spec"
    ]
    require(
        prometheus_ingress["policyTypes"] == ["Ingress"]
        and prometheus_ingress["ingress"][0]["ports"]
        == [{"protocol": "TCP", "port": 9187}]
        and prometheus_ingress["ingress"][0]["from"]
        == [
            {
                "namespaceSelector": {
                    "matchLabels": {
                        "kubernetes.io/metadata.name": "monitoring"
                    }
                },
                "podSelector": {
                    "matchLabels": {
                        "app.kubernetes.io/name": "prometheus",
                        "operator.prometheus.io/name": "prometheus-kube-prometheus-prometheus",
                    }
                },
            }
        ],
        "prod: exporter metrics ingress restricted to Prometheus",
    )

    require(not (prod_directory / "kube-state-metrics-role.yaml").exists(),
            "prod: kube-state-metrics RBAC is owned by kube-prometheus-stack")


# AWS 원본 경로와 Kubernetes Secret의 namespace·키 매핑·보존 정책을 함께 대조한다.
def validate_secrets(path: Path) -> None:
    resources = yaml_documents(path)
    stores = [item for item in resources if item.get("kind") == "SecretStore"]
    external_secrets = [item for item in resources if item.get("kind") == "ExternalSecret"]
    require(len(stores) == 9, f"SecretStore count is {len(stores)}, expected 9")
    require(len(external_secrets) == 37, f"ExternalSecret count is {len(external_secrets)}, expected 37")

    expected_names = {
        "app": set(NONPREVIEW_REQUIRED_SECRETS + ["docs-basic-auth"]),
        "db": {
            "app-db",
            "postgres-secrets",
            "postgres-readonly",
            "postgres-exporter",
            "backup-s3",
        },
        "dev-app": set(NONPREVIEW_REQUIRED_SECRETS + ["docs-basic-auth"]),
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
        "app-webhook": {"DISCORD_WEBHOOK_URL"},
        "docs-basic-auth": {"users"},
        "postgres-exporter": {"POSTGRES_EXPORTER_PASSWORD"},
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
    require(len(sources) == 35, f"Secrets Manager source count is {len(sources)}, expected 35")
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
        len(SECRET_SPECS) == len(uploader_properties) == 35,
        "secret uploader must contain 35 unique sources",
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


def quantity_millicores(value: str) -> int:
    return int(value[:-1]) if value.endswith("m") else int(float(value) * 1000)


def quantity_mebibytes(value: str) -> int:
    for suffix, factor in (("Gi", 1024), ("Mi", 1)):
        if value.endswith(suffix):
            return int(value[: -len(suffix)]) * factor
    raise SystemExit(f"contract validation failed: unsupported memory quantity {value}")


def pod_template_spec(item: dict) -> dict:
    spec = item["spec"]
    if item["kind"] == "CronJob":
        spec = spec["jobTemplate"]["spec"]
    return spec["template"]["spec"]


def container_resource_amounts(container: dict) -> tuple[int, int, int]:
    resources = container["resources"]
    return (
        quantity_millicores(resources["requests"]["cpu"]),
        quantity_mebibytes(resources["requests"]["memory"]),
        quantity_mebibytes(resources["limits"]["memory"]),
    )


# quota는 Pod마다 자원별로 max(일반 container 합, 가장 큰 init container)를 센다.
# init container는 하나씩 끝난 뒤 앱 container가 뜨므로 합하면 quota를 실제보다 크게 잡는다.
def pod_resource_totals(pod: dict) -> tuple[int, int, int]:
    init_containers = pod.get("initContainers", [])
    require(
        all(container.get("restartPolicy") != "Always" for container in init_containers),
        "preview: budget arithmetic does not model sidecar init containers",
    )
    regular = [container_resource_amounts(item) for item in pod["containers"]]
    init = [container_resource_amounts(item) for item in init_containers]
    return tuple(
        max([sum(amounts[index] for amounts in regular)] + [amounts[index] for amounts in init])
        for index in range(3)
    )


# quota 합계를 손으로 맞추지 않도록 실제 PR render와 공용 manifest에서 다시 계산한다.
# PR 한 개의 앱·생성 Job·삭제 Job이 슬롯 2개만큼 동시에 돌고, 공용 DB와 고아 DB 점검이 하나씩 더해진다.
def validate_preview_budget_arithmetic(pr_resources: list[dict]) -> None:
    quota = resource(
        yaml_documents(ROOT / "manifests" / "cluster" / "preview-resourcequota.yaml"),
        "ResourceQuota",
    )["spec"]["hard"]
    preview_directory = ROOT / "manifests" / "postgres" / "preview"
    shared = [
        resource(yaml_documents(preview_directory / "statefulset.yaml"), "StatefulSet"),
        resource(
            yaml_documents(preview_directory / "orphan-database-check-cronjob.yaml"),
            "CronJob",
        ),
    ]
    per_slot = [
        item for item in pr_resources if item.get("kind") in {"Deployment", "Job"}
    ]
    require(
        sorted(item["kind"] for item in per_slot) == ["Deployment", "Job", "Job"],
        "preview: budget expects one Deployment, createdb and dropdb per PR",
    )
    expected = [0, 0, 0]
    for count, items in ((2, per_slot), (1, shared)):
        for item in items:
            for index, value in enumerate(pod_resource_totals(pod_template_spec(item))):
                expected[index] += count * value
    actual = [
        quantity_millicores(quota["requests.cpu"]),
        quantity_mebibytes(quota["requests.memory"]),
        quantity_mebibytes(quota["limits.memory"]),
    ]
    require(
        actual == expected,
        f"preview: quota {actual} must equal two PR slots plus shared workloads {expected}",
    )
    storage = shared[0]["spec"]["volumeClaimTemplates"][0]["spec"]["resources"]["requests"]["storage"]
    require(
        quota["requests.storage"] == storage
        and not any(item.get("kind") == "PersistentVolumeClaim" for item in pr_resources),
        "preview: only the shared PostgreSQL PVC fits the storage quota",
    )


# PR별 앱 수와 공용 DB·wildcard 인증서 구조가 Preview 자원 예산을 넘지 않게 한다.
def validate_preview_budget() -> None:
    quota = resource(
        yaml_documents(ROOT / "manifests" / "cluster" / "preview-resourcequota.yaml"),
        "ResourceQuota",
    )
    # CPU·memory 합계는 validate_preview_budget_arithmetic이 render에서 다시 계산해 비교한다.
    computed = {"requests.cpu", "requests.memory", "limits.memory"}
    hard = quota["spec"]["hard"]
    require(
        computed <= hard.keys()
        and {key: value for key, value in hard.items() if key not in computed}
        == {
            "count/certificates.cert-manager.io": "2",
            "count/services": "4",
            "count/deployments.apps": "2",
            "count/persistentvolumeclaims": "1",
            "requests.storage": "5Gi",
        },
        "preview: two-slot resource budget",
    )
    applicationset = resource(
        yaml_documents(ROOT / "argocd" / "applications" / "preview" / "applicationset.yaml"),
        "ApplicationSet",
    )
    parameters = {
        item["name"]: item["value"]
        for item in applicationset["spec"]["template"]["spec"]["source"]["helm"]["parameters"]
    }
    require(
        parameters.get("deployment.enabled") == "true"
        and parameters.get("ingress.enabled") == "true",
        "preview: ApplicationSet must enable the PR Deployment and Ingress together",
    )
    require(
        parameters.get("env.DEMODAY_QR_BASE_URL")
        == "https://university.neordinary.com",
        "preview: QR frontend origin must use the approved shared web address",
    )
    require(
        parameters.get("ingress.host")
        == "api-pr-{{ .number }}.university.neordinary.com",
        "preview: exact public hostname",
    )
    require(
        "certificate.enabled" not in parameters,
        "preview: ApplicationSet must not create per-PR Certificates",
    )
    require(
        "ingress.tls[0].secretName" not in parameters,
        "preview: ApplicationSet must not generate per-PR TLS Secret names",
    )
    require(
        parameters.get("env.SSO_ISSUER")
        == "https://api-pr-{{ .number }}.university.neordinary.com",
        "preview: dynamic SSO issuer",
    )
    require(
        parameters.get("env.CERTIFICATE_VERIFICATION_URL_TEMPLATE")
        == "https://api-pr-{{ .number }}.university.neordinary.com/api/v1/certificates/verify/{serialNumber}",
        "preview: dynamic certificate verification URL",
    )
    require(
        parameters.get("ingress.tls[0].hosts[0]") == parameters["ingress.host"],
        "preview: TLS hostname",
    )
    preview_values = yaml.safe_load(
        (ROOT / "charts" / "umc-product-server" / "values-preview.yaml").read_text(
            encoding="utf-8"
        )
    )
    require(
        preview_values["deployment"]["enabled"] is False
        and preview_values["ingress"]["enabled"] is False,
        "preview: standalone values must remain disabled without PR metadata",
    )
    require(
        preview_values["certificate"]["enabled"] is False,
        "preview: chart must disable per-PR Certificates",
    )
    require(
        preview_values["ingress"]["tls"]
        == [
            {
                "hosts": ["preview.example.invalid"],
                "secretName": "preview-wildcard-tls",
            }
        ],
        "preview: all Ingresses must use the shared wildcard TLS Secret",
    )


# 일시적인 sync 실패는 platform 앱과 같은 간격으로 재시도한다. createdb는 완료 표시로 재실행에 안전하다.
SERVER_SYNC_RETRY = {
    "limit": 5,
    "backoff": {"duration": "10s", "factor": 2, "maxDuration": "3m"},
}


# Reloader가 바꾸는 annotation만 Git 비교에서 제외해 다른 Deployment 차이는 계속 탐지한다.
def validate_reloader_argocd_contract() -> None:
    expected_applications = {
        "prod/server.yaml": ("umc-product-server", "app"),
        "dev/server.yaml": ("umc-product-server", "dev-app"),
    }
    for relative_path, (deployment_name, namespace) in expected_applications.items():
        application = resource(
            yaml_documents(ROOT / "argocd" / "applications" / relative_path),
            "Application",
        )
        spec = application["spec"]
        expected_rule = {
            "group": "apps",
            "kind": "Deployment",
            "name": deployment_name,
            "namespace": namespace,
            "jsonPointers": [RELOADER_RUNTIME_POINTER],
        }
        require(
            spec.get("ignoreDifferences") == [expected_rule],
            f"{namespace}: narrow Reloader runtime annotation ignore",
        )
        require(
            "RespectIgnoreDifferences=true"
            in spec["syncPolicy"].get("syncOptions", []),
            f"{namespace}: Argo sync must respect Reloader ignore rule",
        )
        require(
            spec["syncPolicy"].get("retry") == SERVER_SYNC_RETRY,
            f"{namespace}: server Application must retry transient sync failures",
        )

    applicationset = resource(
        yaml_documents(
            ROOT / "argocd" / "applications" / "preview" / "applicationset.yaml"
        ),
        "ApplicationSet",
    )
    spec = applicationset["spec"]["template"]["spec"]
    expected_rule = {
        "group": "apps",
        "kind": "Deployment",
        "name": "umc-product-server-pr-{{ .number }}",
        "namespace": "preview",
        "jsonPointers": [RELOADER_RUNTIME_POINTER],
    }
    require(
        spec.get("ignoreDifferences") == [expected_rule],
        "preview: narrow Reloader runtime annotation ignore",
    )
    require(
        "RespectIgnoreDifferences=true" in spec["syncPolicy"].get("syncOptions", []),
        "preview: Argo sync must respect Reloader ignore rule",
    )
    require(
        spec["syncPolicy"].get("retry") == SERVER_SYNC_RETRY,
        "preview: ApplicationSet must retry transient sync failures",
    )


# 필수 디렉터리와 실제 Git 입력을 확인해 로컬 잔여 파일에 의존하는 checkout을 막는다.
def validate_repository_identity() -> None:
    required_directories = [
        "ansible",
        "argocd/applications",
        "bootstrap",
        "charts/umc-product-server",
        "charts/umc-secrets",
        "cloud/aws",
        "manifests/cluster",
        "manifests/cert-manager",
        "manifests/postgres/prod",
        "manifests/postgres/dev",
        "manifests/postgres/preview",
        "manifests/observability",
        "observability",
        "docs/runbooks",
    ]
    for directory in required_directories:
        require((ROOT / directory).is_dir(), f"missing directory: {directory}")
    repository_files = git_visible_files(ROOT)
    require(
        not [path for path in repository_files if path.suffix == ".tf"],
        "Terraform files are not allowed",
    )

    stale: list[str] = []
    for path in repository_files:
        if not path.is_file() or path.suffix.lower() in {".png"}:
            continue
        try:
            content = path.read_text(encoding="utf-8").lower()
        except UnicodeDecodeError:
            continue
        if any(identifier in content for identifier in LEGACY_IDENTIFIERS):
            stale.append(str(path.relative_to(ROOT)))
    require(not stale, f"legacy identity remains in: {stale}")

    for path in (ROOT / "argocd").rglob("*.yaml"):
        for document in yaml_documents(path):
            if document.get("kind") == "Application":
                source = document["spec"]["source"]
                if source.get("repoURL") == "https://github.com/UMC-PRODUCT/umc-product-infra.git":
                    require((ROOT / source["path"]).exists(), f"missing Argo path: {source['path']}")
            elif document.get("kind") == "ApplicationSet":
                source = document["spec"]["template"]["spec"]["source"]
                require(
                    source["repoURL"] == "https://github.com/UMC-PRODUCT/umc-product-infra.git",
                    "ApplicationSet repository",
                )
                require((ROOT / source["path"]).exists(), f"missing Argo path: {source['path']}")


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
        (ROOT / "charts" / "umc-product-server" / "values.yaml").read_text(
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
    for environment, filename in zip(("prod", "dev", "preview"), sys.argv[1:4]):
        overlay = yaml.safe_load(
            (
                ROOT
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

#!/usr/bin/env python3
"""Validate UMC-specific contracts that generic YAML/schema tools cannot express."""

from __future__ import annotations

import ipaddress
import os
import re
import subprocess
import sys
from pathlib import Path

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
PREVIEW_APP_IMAGE = "ghcr.io/umc-product/umc-product-server:0123456789ab"
TEST_NET_EDGE_TARGET = "203.0.113.10"
CERTIFICATE_ISSUERS = {"letsencrypt-staging", "letsencrypt-production"}
GOOGLE_CLIENT_ID_LIST = (
    "882297658822-ag179p23eqhc5lti8ue5cgbf5phekbfh.apps.googleusercontent.com,"
    "882297658822-qaf493b6fu6a9f33artnu435jimkoome.apps.googleusercontent.com,"
    "882297658822-gsl0u8uo78qtt5h0ggkm421sml9c02pd.apps.googleusercontent.com,"
    "882297658822-d47kcffbv3eoms278pak6rcv21lhcei8.apps.googleusercontent.com,"
    "655033275728-f7jpdingakk1f4im8reegjmioth8jvnv.apps.googleusercontent.com"
)
BASE_REQUIRED_SECRETS = ["app-db", "app-jwt", "app-oauth", "app-storage", "app-email"]
PROD_REQUIRED_SECRETS = [*BASE_REQUIRED_SECRETS, "app-fcm"]
BASE_RELOADER_SECRETS = ["app-oauth", "app-storage", "app-email"]
PROD_RELOADER_SECRETS = [*BASE_RELOADER_SECRETS, "app-fcm"]
RELOADER_TRIGGER_ANNOTATION = "secret.reloader.stakater.com/reload"
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


def validate_route53_contract() -> None:
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
                        "route53:ChangeResourceRecordSetsRecordTypes": ["A", "TXT"],
                    },
                    "ForAllValues:StringLike": {
                        "route53:ChangeResourceRecordSetsNormalizedRecordNames": [
                            "api.${DnsZoneName}",
                            "api-dev.${DnsZoneName}",
                            "api-pr-*.${DnsZoneName}",
                            "grafana.${DnsZoneName}",
                            "argo.${DnsZoneName}",
                            "_external-dns.a-api.${DnsZoneName}",
                            "_external-dns.a-api-dev.${DnsZoneName}",
                            "_external-dns.a-api-pr-*.${DnsZoneName}",
                            "_external-dns.a-grafana.${DnsZoneName}",
                            "_external-dns.a-argo.${DnsZoneName}",
                        ]
                    },
                },
            },
        },
        "ExternalDNS Route53 least-privilege policy",
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
    ]
    expected_nonprod_origins = [*expected_prod_origins, "http://localhost:5173"]
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
        and route53_parameter.get("MinLength") == 1
        and route53_parameter.get("MaxLength") == 32
        and route53_parameter.get("AllowedPattern") == "^[A-Z0-9]{1,32}$",
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
        },
        "SES Route53 record set",
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


def resource(resources: list[dict], kind: str) -> dict:
    matches = [item for item in resources if item.get("kind") == kind]
    require(len(matches) == 1, f"expected one {kind}, found {len(matches)}")
    return matches[0]


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
        },
        f"{environment}: exact Route53-backed Ingress annotations",
    )


def validate_documentation_ingress_contract(
    ingress: dict,
    middleware: dict,
    environment: str,
) -> None:
    expected_host, expected_tls_secret, expected_service = edge_expectation(environment)
    expected_namespace = "app" if environment == "prod" else "dev-app"
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
            "traefik.ingress.kubernetes.io/router.middlewares": (
                f"{expected_namespace}-{middleware_name}@kubernetescrd"
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
            "traefik.ingress.kubernetes.io/router.middlewares": (
                f"dev-app-{middleware_name}@kubernetescrd"
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
    require(
        service["spec"]["type"] == "ClusterIP"
        and service["spec"]["ports"] == [
            {"name": "http", "port": 80, "targetPort": "http", "protocol": "TCP"}
        ],
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
        PROD_REQUIRED_SECRETS if environment == "prod" else BASE_REQUIRED_SECRETS
    )
    require(
        [item["secretRef"]["name"] for item in container["envFrom"]]
        == expected_required_secrets,
        f"{environment}: required Secret order",
    )
    expected_reloader_secrets = (
        PROD_RELOADER_SECRETS
        if environment == "prod"
        else BASE_RELOADER_SECRETS
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
            "CERTIFICATE_VERIFICATION_URL_TEMPLATE": "https://api.university.neordinary.com/api/v1/certificates/verify/{serialNumber}",
            "S3_BUCKET_NAME": "umc-product-prod-app-storage-351284652562-ap-northeast-2",
            "GOOGLE_CLIENT_ID_LIST": GOOGLE_CLIENT_ID_LIST,
            "CORS_ALLOWED_ORIGIN_PATTERNS": (
                "https://university.neordinary.com,"
                "https://admin.university.neordinary.com,"
                "https://tech.university.neordinary.com"
            ),
            "DATABASE_USERNAME": "umc_product_app",
            "DATABASE_URL": "jdbc:postgresql://postgres.db.svc.cluster.local:5432/umc_product",
        },
        "dev": {
            "APP_SEED_ENABLED": "true",
            "APP_TEST_API_ENABLED": "true",
            "FCM_ENABLED": "false",
            "OPENAPI_ENABLE": "true",
            "SPRING_PROFILES_ACTIVE": "dev",
            "SPRING_APPLICATION_NAME": "dev-umc-product",
            "APP_ENVIRONMENT": "dev",
            "SSO_ISSUER": "https://api-dev.university.neordinary.com",
            "CERTIFICATE_VERIFICATION_URL_TEMPLATE": "https://api-dev.university.neordinary.com/api/v1/certificates/verify/{serialNumber}",
            "S3_BUCKET_NAME": "umc-product-dev-app-storage-351284652562-ap-northeast-2",
            "GOOGLE_CLIENT_ID_LIST": GOOGLE_CLIENT_ID_LIST,
            "DATABASE_USERNAME": "umc_product_dev_app",
            "DATABASE_URL": "jdbc:postgresql://postgres.dev-db.svc.cluster.local:5432/umc_product_dev",
        },
        "preview": {
            "APP_SEED_ENABLED": "false",
            "APP_TEST_API_ENABLED": "false",
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

    provider = environment_values.get("EMAIL_PROVIDER")
    require(provider in {"ses", "smtp"}, f"{environment}: email provider")
    require("SMTP_PASSWORD" not in environment_values, "SMTP password must come from Secret")
    if provider == "smtp":
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

    documentation_enabled = environment in {"prod", "dev"}
    ingresses = [item for item in resources if item.get("kind") == "Ingress"]
    expected_ingress_count = (
        (1 + int(documentation_enabled) + int(test_api_enabled))
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
            int(documentation_enabled) + int(test_api_enabled)
            if ingress_enabled
            else 0
        ),
        f"{environment}: protected-route Middleware must match source gates",
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
        jobs = [item for item in resources if item.get("kind") == "Job"]
        require(len(jobs) == 2, "preview: createdb and dropdb Jobs")
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
    require(
        not any(item.get("kind") == "Certificate" for item in resources),
        "active preview edge fixture: per-PR Certificate must stay disabled",
    )
    validate_ingress_contract(
        resource(resources, "Ingress"),
        "preview",
        TEST_NET_EDGE_TARGET,
        test_net_fixture=True,
    )


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
        if environment == "preview":
            require(
                "max_connections=60" in container.get("args", []),
                "preview: connection budget must match three slots",
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

    prod_directory = ROOT / "manifests" / "postgres" / "prod"
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


def validate_secrets(path: Path) -> None:
    resources = yaml_documents(path)
    stores = [item for item in resources if item.get("kind") == "SecretStore"]
    external_secrets = [item for item in resources if item.get("kind") == "ExternalSecret"]
    require(len(stores) == 9, f"SecretStore count is {len(stores)}, expected 9")
    require(len(external_secrets) == 31, f"ExternalSecret count is {len(external_secrets)}, expected 31")

    expected_names = {
        "app": set(PROD_REQUIRED_SECRETS + ["docs-basic-auth"]),
        "db": {
            "app-db",
            "postgres-secrets",
            "postgres-readonly",
            "postgres-exporter",
            "backup-s3",
        },
        "dev-app": set(BASE_REQUIRED_SECRETS + ["docs-basic-auth"]),
        "dev-db": {"app-db", "postgres-secrets"},
        "preview": set(BASE_REQUIRED_SECRETS + ["postgres-preview-secrets"]),
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
        if name == "app-email" and item["metadata"]["namespace"] in {"app", "dev-app"}:
            expected["SMTP_PASSWORD"] = "SMTP_PASSWORD"
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
    require(len(sources) == 29, f"Secrets Manager source count is {len(sources)}, expected 29")

    uploader_properties = {
        spec.path: {property_name for property_name, _env_key in spec.properties}
        for spec in SECRET_SPECS
    }
    require(
        uploader_properties == source_properties,
        "secret uploader paths/properties must match rendered ExternalSecrets",
    )
    require(
        len(SECRET_SPECS) == len(uploader_properties) == 29,
        "secret uploader must contain 29 unique sources",
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


def validate_preview_budget() -> None:
    quota = resource(
        yaml_documents(ROOT / "manifests" / "cluster" / "preview-resourcequota.yaml"),
        "ResourceQuota",
    )
    require(
        quota["spec"]["hard"]
        == {
            "count/certificates.cert-manager.io": "1",
            "count/services": "5",
            "count/deployments.apps": "3",
            "requests.cpu": "1050m",
            "requests.memory": "4416Mi",
            "limits.memory": "7808Mi",
        },
        "preview: three-slot resource budget",
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
        "runbooks",
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


def main() -> int:
    if len(sys.argv) != 7:
        raise SystemExit(
            "usage: validate_contracts.py PROD_RENDER DEV_RENDER PREVIEW_RENDER "
            "SECRETS_RENDER ACTIVE_EDGE_FIXTURE ACTIVE_PREVIEW_EDGE_FIXTURE"
        )
    validate_repository_identity()
    validate_route53_contract()
    validate_app_storage_contract()
    validate_ses_contract()
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
            test_api_enabled is (environment == "dev"),
            f"{environment}: test API must be enabled only in dev",
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

"""Route 53 delegation and ExternalDNS record ownership contracts."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from contracts import common
from contracts.aws import AWS_REGION
from contracts.common import CloudFormationLoader, require, yaml_documents


# validate.sh가 Preview 렌더 견본으로 쓰는 PR 번호다.
PREVIEW_SAMPLE_PR_NUMBER = "42"


def external_dns_registry_config() -> dict:
    application = yaml.safe_load(
        (common.ROOT / "argocd" / "applications" / "platform" / "external-dns.yaml").read_text(
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
    require(bool(hosts), "ExternalDNS: no opted-in Ingress host was collected")
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
        for path in sorted((common.ROOT / "manifests").rglob("*.yaml"))
        for document in yaml_documents(path)
    ]
    return external_dns_hosts(documents, annotation_filter)


# 기존 위임 zone을 재생성하지 않고 인증서용 TXT와 앱용 DNS 변경 권한을 분리하는지 확인한다.
def validate_route53_contract(render_paths: list[Path]) -> None:
    template_path = common.ROOT / "cloud" / "aws" / "route53-dns.yaml"
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

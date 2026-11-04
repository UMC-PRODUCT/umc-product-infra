"""S3 app storage, SES, and CloudFormation tag contracts."""

from __future__ import annotations

import yaml

from contracts.common import ROOT, CloudFormationLoader, require


AWS_ACCOUNT_ID = "351284652562"
AWS_REGION = "ap-northeast-2"


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

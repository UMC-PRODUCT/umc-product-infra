"""umc-product-server chart render and Argo CD Application contracts."""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

import yaml

from contracts import common
from contracts.common import require, resource, yaml_documents
from contracts.edge import (
    edge_expectation,
    validate_documentation_ingress_contract,
    validate_edge_protection,
    validate_ingress_contract,
    validate_test_api_ingress_contract,
)
from contracts.external_secrets import (
    BASE_REQUIRED_SECRETS,
    DEV_REQUIRED_SECRETS,
    NONPREVIEW_REQUIRED_SECRETS,
)
from contracts.postgres import POSTGIS_IMAGE
from contracts.preview_budget import validate_preview_budget_arithmetic


APP_IMAGE = (
    "ghcr.io/umc-product/umc-product-server@sha256:"
    + "a" * 64
)
PREVIEW_APP_IMAGE = APP_IMAGE


CERTIFICATE_ISSUERS = {"letsencrypt-staging", "letsencrypt-production"}
KAKAO_JAVASCRIPT_KEY = "4e79ba06223b60e13ad76bb6aec8f2b9"
GOOGLE_CLIENT_ID_LIST = (
    "882297658822-ag179p23eqhc5lti8ue5cgbf5phekbfh.apps.googleusercontent.com,"
    "882297658822-qaf493b6fu6a9f33artnu435jimkoome.apps.googleusercontent.com,"
    "882297658822-gsl0u8uo78qtt5h0ggkm421sml9c02pd.apps.googleusercontent.com,"
    "882297658822-d47kcffbv3eoms278pak6rcv21lhcei8.apps.googleusercontent.com,"
    "655033275728-f7jpdingakk1f4im8reegjmioth8jvnv.apps.googleusercontent.com"
)


BASE_RELOADER_SECRETS = ["app-oauth", "app-storage", "app-email"]
NONPREVIEW_RELOADER_SECRETS = [*BASE_RELOADER_SECRETS, "app-fcm", "app-report-discord"]
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
        for path in sorted((common.ROOT / "manifests" / "cert-manager").glob("*.yaml"))
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
    builder: dict = next(
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
    expected_required_secrets = {
        "prod": NONPREVIEW_REQUIRED_SECRETS,
        "dev": DEV_REQUIRED_SECRETS,
        "preview": BASE_REQUIRED_SECRETS,
    }[environment]
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
    shared_environment = {
        "MANAGEMENT_ENDPOINT_HEALTH_PROBES_ENABLED": "true",
        "MANAGEMENT_SERVER_PORT": "9090",
        "OTEL_URL": "http://otel-collector.monitoring.svc.cluster.local:4318",
        "TRACE_SAMPLING_PROBABILITY": "1.0",
        "DEMODAY_QR_BASE_URL": "https://validation.example.com",
        "S3_REGION": "ap-northeast-2",
        "SES_REGION": "ap-northeast-2",
    }
    for key, expected in shared_environment.items():
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
            "APP_SEED_QA_AUTO_RUN": "true",
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
            "APP_SEED_ENABLED": "false",
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
    if environment != "dev":
        require(
            "APP_SEED_QA_AUTO_RUN" not in environment_values,
            f"{environment}: QA seed auto-run must stay dev-only",
        )

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
    require(
        "APP_SEED_DEFAULT_PASSWORD" not in environment_values,
        "seed account password must come from Secret",
    )
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
            (common.ROOT / "charts" / "umc-product-server" / "values.yaml").read_text(
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
        validate_preview_dropdb_job(
            next(
                item
                for item in jobs
                if item["metadata"]["labels"]["app.kubernetes.io/component"]
                == "database-cleanup"
            )
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


def validate_preview_dropdb_job(dropdb: dict) -> None:
    require(
        dropdb["metadata"]["annotations"]["argocd.argoproj.io/hook"] == "PostDelete",
        "preview: dropdb must run after workload deletion",
    )
    container = dropdb["spec"]["template"]["spec"]["containers"][0]
    require(
        'DROP DATABASE IF EXISTS :"db" WITH (FORCE);' in container["args"][0],
        "preview: dropdb must force-disconnect only the quoted target database",
    )
    # 연결 대기의 순서와 상한은 scripts/tests의 실행 테스트가 확인한다. 여기서는 그 대기가
    # 동작할 실행 환경만 고정한다. image의 sh(dash)에는 $SECONDS가 없다.
    require(
        container["command"] == ["bash", "-c"],
        "preview: dropdb must run under bash so the $SECONDS connection wait is bounded",
    )
    require(
        dropdb["spec"]["activeDeadlineSeconds"] >= 900,
        "preview: dropdb deadline must cover the 600s connection wait and the DROP",
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
            yaml_documents(common.ROOT / "argocd" / "applications" / relative_path),
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
            common.ROOT / "argocd" / "applications" / "preview" / "applicationset.yaml"
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

"""Public Ingress host, Traefik middleware, and edge protection contracts."""

from __future__ import annotations

import ipaddress
from pathlib import Path

from contracts.common import require, resource, yaml_documents


TEST_NET_EDGE_TARGET = "203.0.113.10"
APP_NAMESPACES = {"prod": "app", "dev": "dev-app", "preview": "preview"}


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
            "api-pr-42.preview.university.neordinary.com",
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
        mail_ingress["spec"]["rules"][0]["host"] == "mail-pr-42.preview.university.neordinary.com"
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

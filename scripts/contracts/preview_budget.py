"""Preview namespace resource quota budget contracts."""

from __future__ import annotations

import yaml

from contracts import common
from contracts.common import require, resource, yaml_documents


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
    cpu, memory, memory_limit = (
        max([sum(amounts[index] for amounts in regular)] + [amounts[index] for amounts in init])
        for index in range(3)
    )
    return cpu, memory, memory_limit


# quota 합계를 손으로 맞추지 않도록 실제 PR render와 공용 manifest에서 다시 계산한다.
# PR 한 개의 앱·생성 Job·삭제 Job이 슬롯 2개만큼 동시에 돌고, 공용 DB와 고아 DB 점검이 하나씩 더해진다.
def validate_preview_budget_arithmetic(pr_resources: list[dict]) -> None:
    quota = resource(
        yaml_documents(common.ROOT / "manifests" / "cluster" / "preview-resourcequota.yaml"),
        "ResourceQuota",
    )["spec"]["hard"]
    preview_directory = common.ROOT / "manifests" / "postgres" / "preview"
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
        yaml_documents(common.ROOT / "manifests" / "cluster" / "preview-resourcequota.yaml"),
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
        yaml_documents(common.ROOT / "argocd" / "applications" / "preview" / "applicationset.yaml"),
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
        == "api-pr-{{ .number }}.preview.university.neordinary.com",
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
        == "https://api-pr-{{ .number }}.preview.university.neordinary.com",
        "preview: dynamic SSO issuer",
    )
    require(
        parameters.get("env.CERTIFICATE_VERIFICATION_URL_TEMPLATE")
        == "https://api-pr-{{ .number }}.preview.university.neordinary.com/api/v1/certificates/verify/{serialNumber}",
        "preview: dynamic certificate verification URL",
    )
    require(
        parameters.get("ingress.tls[0].hosts[0]") == parameters["ingress.host"],
        "preview: TLS hostname",
    )
    preview_values = yaml.safe_load(
        (common.ROOT / "charts" / "umc-product-server" / "values-preview.yaml").read_text(
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

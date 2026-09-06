#!/usr/bin/env python3
"""Render and validate the pinned Argo CD bootstrap Helm release."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULTS_PATH = ROOT / "ansible" / "roles" / "argocd" / "defaults" / "main.yml"
VALUES_PATH = ROOT / "ansible" / "roles" / "argocd" / "files" / "argo-cd-values.yaml"
EXPECTED_WORKLOADS = {
    ("Deployment", "argocd-applicationset-controller"),
    ("Deployment", "argocd-dex-server"),
    ("Deployment", "argocd-redis"),
    ("Deployment", "argocd-repo-server"),
    ("Deployment", "argocd-server"),
    ("Job", "argocd-redis-secret-init"),
    ("StatefulSet", "argocd-application-controller"),
}
EXPECTED_CRDS = {
    "applications.argoproj.io",
    "applicationsets.argoproj.io",
    "appprojects.argoproj.io",
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"Argo CD validation failed: {message}")


def run(
    command: list[str], *, stdout: object = subprocess.PIPE
) -> subprocess.CompletedProcess:
    completed = subprocess.run(
        command,
        text=True,
        stdout=stdout,
        stderr=subprocess.PIPE,
        check=False,
    )
    require(completed.returncode == 0, f"{' '.join(command)}\n{completed.stderr}")
    return completed


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    defaults = yaml.safe_load(DEFAULTS_PATH.read_text(encoding="utf-8"))
    values = yaml.safe_load(VALUES_PATH.read_text(encoding="utf-8"))

    chart_version = str(defaults["argocd_helm_chart_version"])
    app_version = str(defaults["argocd_version"])
    helm_version = str(defaults["argocd_helm_version"])
    expected_checksum = str(defaults["argocd_helm_chart_checksum"]).removeprefix(
        "sha256:"
    )
    require(
        values["global"]["image"]["tag"] == app_version,
        "values image tag must match argocd_version",
    )
    require(
        values["global"]["domain"] == "argocd.invalid",
        "the non-public bootstrap must not use the chart example domain",
    )
    actual_helm_version = run(["helm", "version", "--short"]).stdout.strip()
    require(
        actual_helm_version == helm_version
        or actual_helm_version.startswith(f"{helm_version}+"),
        f"Helm version must be {helm_version}, got {actual_helm_version}",
    )

    archive = output_dir / f"argo-cd-{chart_version}.tgz"
    request = urllib.request.Request(
        str(defaults["argocd_helm_chart_url"]),
        headers={"User-Agent": "umc-infra-validator"},
    )
    with urllib.request.urlopen(request, timeout=60) as response, archive.open(
        "wb"
    ) as stream:
        shutil.copyfileobj(response, stream)
    require(sha256(archive) == expected_checksum, "chart archive checksum")

    metadata_output = run(["helm", "show", "chart", str(archive)]).stdout
    metadata = yaml.safe_load(metadata_output)
    require(str(metadata.get("version")) == chart_version, "chart version")
    require(str(metadata.get("appVersion")) == app_version, "chart appVersion")

    run(["helm", "lint", str(archive), "--strict", "-f", str(VALUES_PATH)])
    rendered_path = output_dir / "argocd.yaml"
    with rendered_path.open("w", encoding="utf-8") as stream:
        run(
            [
                "helm",
                "template",
                "argocd",
                str(archive),
                "--namespace",
                "argocd",
                "--kube-version",
                "1.36.0",
                "--include-crds",
                "-f",
                str(VALUES_PATH),
            ],
            stdout=stream,
        )

    with rendered_path.open(encoding="utf-8") as stream:
        documents = [
            item for item in yaml.safe_load_all(stream) if isinstance(item, dict)
        ]
    require(
        "argocd.example.com" not in rendered_path.read_text(encoding="utf-8"),
        "chart example domain leaked into rendered resources",
    )
    workloads = {
        (item.get("kind"), item.get("metadata", {}).get("name")): item
        for item in documents
        if item.get("kind") in {"Deployment", "Job", "StatefulSet"}
    }
    require(set(workloads) == EXPECTED_WORKLOADS, "workload set drifted")

    for (kind, name), workload in workloads.items():
        pod_spec = workload["spec"]["template"]["spec"]
        containers = pod_spec.get("initContainers", []) + pod_spec.get("containers", [])
        require(containers, f"{kind}/{name}: no containers")
        for container in containers:
            resources = container.get("resources", {})
            for boundary in ("requests", "limits"):
                require(
                    {"cpu", "memory"} <= set(resources.get(boundary, {})),
                    f"{kind}/{name}/{container.get('name')}: incomplete {boundary}",
                )

    repo_server = workloads[("Deployment", "argocd-repo-server")]
    require(
        repo_server["spec"]["template"]["spec"].get("automountServiceAccountToken")
        is False,
        "repo-server Pod must not receive Kubernetes API credentials",
    )
    repo_service_accounts = [
        item
        for item in documents
        if item.get("kind") == "ServiceAccount"
        and item.get("metadata", {}).get("name") == "argocd-repo-server"
    ]
    require(len(repo_service_accounts) == 1, "repo-server ServiceAccount")
    require(
        repo_service_accounts[0].get("automountServiceAccountToken") is False,
        "repo-server ServiceAccount must disable token automount",
    )
    redis = workloads[("Deployment", "argocd-redis")]
    require(
        redis["spec"]["template"]["spec"].get("automountServiceAccountToken") is False,
        "Redis Pod must not receive Kubernetes API credentials",
    )
    require(
        not any(
            item.get("kind") in {"ClusterRole", "ClusterRoleBinding"}
            and item.get("metadata", {}).get("name")
            == "argocd-notifications-controller"
            for item in documents
        ),
        "disabled notifications must not receive cluster-wide Secret access",
    )

    actual_crds = {
        item.get("metadata", {}).get("name")
        for item in documents
        if item.get("kind") == "CustomResourceDefinition"
    }
    require(actual_crds == EXPECTED_CRDS, "CRD set drifted")
    print("Argo CD Helm release validation passed")


if __name__ == "__main__":
    require(len(sys.argv) == 2, "usage: validate_argocd.py OUTPUT_DIR")
    main(Path(sys.argv[1]))

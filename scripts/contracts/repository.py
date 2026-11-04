"""Repository layout and Git-visible content contracts."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from contracts.common import ROOT, require, yaml_documents


LEGACY_IDENTIFIERS = (
    "tap" + "ple",
    "tap" + "le",
    "tap" + "plee",
    "umc" + ".it.kr",
    "umc" + "-it-kr",
)


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

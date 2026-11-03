#!/usr/bin/env python3
"""Argo CD가 Git에서 읽는 원본의 파일 구성과 앱 chart version 갱신을 검증한다."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ARGOCD_DIR = "argocd"
# 파일명 규칙은 validate_preview_releases.py가 검사한다.
PREVIEW_RELEASES_DIR = "argocd/preview-releases/"
CHART_DIR = "charts/umc-product-server"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )


# commit될 수 있는 파일만 본다. .gitignore에 걸린 로컬 .DS_Store는 Argo CD에 닿지 않는다.
def repository_files(root: Path, directory: str) -> list[str]:
    listed = git(root, "ls-files", "--cached", "--others", "--exclude-standard", "--", directory)
    require(listed.returncode == 0, f"git ls-files failed: {listed.stderr.strip()}")
    return sorted({name for name in listed.stdout.splitlines() if (root / name).is_file()})


# root Application은 argocd/ 전체를 recurse로 읽으므로 manifest가 아닌 파일이 섞이면 sync가 깨진다.
def validate_argocd_tree(root: Path) -> int:
    count = 0
    for name in repository_files(root, ARGOCD_DIR):
        if name.startswith(PREVIEW_RELEASES_DIR) or Path(name).name == "README.md":
            continue
        require(name.endswith(".yaml"), f"argocd/에는 .yaml manifest만 둘 수 있음: {name}")
        documents = [item for item in yaml.safe_load_all((root / name).read_text(encoding="utf-8")) if item is not None]
        require(
            bool(documents) and all(isinstance(item, dict) and item.get("kind") and item.get("apiVersion")
                                    for item in documents),
            f"argocd/의 YAML은 kind·apiVersion이 있는 Kubernetes manifest여야 함: {name}",
        )
        count += 1
    return count


def chart_inputs_changed(paths: list[str]) -> list[str]:
    prefixes = (f"{CHART_DIR}/templates/", f"{CHART_DIR}/files/")
    return [
        path for path in paths
        if path.startswith(prefixes)
        or (Path(path).parent.as_posix() == CHART_DIR and Path(path).name.startswith("values")
            and path.endswith(".yaml"))
    ]


def chart_version(text: str) -> str:
    return str(yaml.safe_load(text)["version"])


# chart 입력이 바뀌었는데 version이 그대로면 Argo CD가 같은 chart version으로 다른 내용을 배포한다.
# 여러 PR이 동시에 bump하므로 고정 숫자 대신 merge base의 version과 비교한다.
def validate_chart_version_bump(root: Path, base_ref: str) -> str:
    merge_base = git(root, "merge-base", base_ref, "HEAD")
    require(merge_base.returncode == 0, f"{base_ref}와 HEAD의 merge base를 찾을 수 없음")
    base = merge_base.stdout.strip()
    diff = git(root, "diff", "--name-only", base, "--", CHART_DIR)
    require(diff.returncode == 0, f"git diff failed: {diff.stderr.strip()}")
    untracked = git(root, "ls-files", "--others", "--exclude-standard", "--", CHART_DIR)
    changed = chart_inputs_changed(sorted(set(diff.stdout.splitlines() + untracked.stdout.splitlines())))
    if not changed:
        return f"chart inputs unchanged since {base_ref}"
    base_chart = git(root, "show", f"{base}:{CHART_DIR}/Chart.yaml")
    if base_chart.returncode != 0:
        return f"{CHART_DIR} is new since {base_ref}"
    before = chart_version(base_chart.stdout)
    after = chart_version((root / CHART_DIR / "Chart.yaml").read_text(encoding="utf-8"))
    require(
        before != after,
        f"{CHART_DIR}/Chart.yaml version {after}를 올려야 함 ({base_ref} 이후 변경: {', '.join(changed)})",
    )
    return f"chart version {before} -> {after} covers {len(changed)} changed chart inputs"


# CI PR은 VALIDATE_BASE_REF로 base를 넘긴다. main push와 base가 없는 로컬 clone은 건너뛴다.
def resolve_base_ref(root: Path, environ: dict[str, str]) -> str | None:
    explicit = environ.get("VALIDATE_BASE_REF", "")
    if explicit:
        return explicit
    if environ.get("GITHUB_ACTIONS") == "true":
        return None
    fallback = "origin/main"
    if git(root, "rev-parse", "--verify", "--quiet", f"{fallback}^{{commit}}").returncode != 0:
        return None
    return fallback


def main() -> None:
    inside = git(ROOT, "rev-parse", "--is-inside-work-tree")
    require(inside.returncode == 0, "Git 작업 트리에서 실행해야 함")
    count = validate_argocd_tree(ROOT)
    print(f"argocd/ holds only Kubernetes manifests ({count} files) outside preview-releases/")
    base_ref = resolve_base_ref(ROOT, dict(os.environ))
    if base_ref is None:
        print("chart version guard: no base ref (main push or no origin/main); not compared")
        return
    print(validate_chart_version_bump(ROOT, base_ref))


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        sys.exit(f"GitOps source validation failed: {error}")

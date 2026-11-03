#!/usr/bin/env python3
"""Argo CD가 Git에서 읽는 원본의 파일 구성과 앱 chart version 갱신을 검증한다."""

from __future__ import annotations

import argparse
import fnmatch
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ROOT_APP_PATH = "bootstrap/root-app.yaml"
CHART_DIR = "charts/umc-product-server"
CHART_FILE = f"{CHART_DIR}/Chart.yaml"


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


# root Application이 실제로 읽는 경로와 exclude를 그대로 쓴다. Argo CD는 {a,b} 형태로 여러 pattern을 받는다.
def root_app_source(root: Path) -> tuple[str, list[str]]:
    source = yaml.safe_load((root / ROOT_APP_PATH).read_text(encoding="utf-8"))["spec"]["source"]
    exclude = str(source.get("directory", {}).get("exclude", ""))
    if exclude.startswith("{") and exclude.endswith("}"):
        exclude = exclude[1:-1]
    return str(source["path"]), [pattern for pattern in exclude.split(",") if pattern]


# root Application은 path 전체를 recurse로 읽으므로 manifest가 아닌 파일이 섞이면 sync가 깨진다.
# exclude된 preview-releases/의 파일명 규칙은 validate_preview_releases.py가 검사한다.
def validate_argocd_tree(root: Path) -> int:
    directory, excludes = root_app_source(root)
    count = 0
    for name in repository_files(root, directory):
        relative = Path(name).relative_to(directory).as_posix()
        if any(fnmatch.fnmatchcase(relative, pattern) for pattern in excludes) or Path(name).name == "README.md":
            continue
        require(name.endswith(".yaml"), f"{directory}/에는 .yaml manifest만 둘 수 있음: {name}")
        documents = [item for item in yaml.safe_load_all((root / name).read_text(encoding="utf-8")) if item is not None]
        require(
            bool(documents) and all(isinstance(item, dict) and item.get("kind") and item.get("apiVersion")
                                    for item in documents),
            f"{directory}/의 YAML은 kind·apiVersion이 있는 Kubernetes manifest여야 함: {name}",
        )
        count += 1
    return count


def chart_version(text: str) -> str:
    return str(yaml.safe_load(text)["version"])


# Chart.yaml 밖의 chart 파일이 바뀌었는데 version이 그대로면 Argo CD가 같은 chart version으로 다른 내용을 배포한다.
# 여러 PR이 동시에 bump하므로 고정 숫자 대신 merge base의 version과 비교한다.
def validate_chart_version_bump(root: Path, base_ref: str) -> str:
    merge_base = git(root, "merge-base", base_ref, "HEAD")
    require(merge_base.returncode == 0, f"{base_ref}와 HEAD의 merge base를 찾을 수 없음")
    base = merge_base.stdout.strip()
    diff = git(root, "diff", "--name-only", base, "--", CHART_DIR)
    require(diff.returncode == 0, f"git diff failed: {diff.stderr.strip()}")
    untracked = git(root, "ls-files", "--others", "--exclude-standard", "--", CHART_DIR)
    changed = sorted(set(diff.stdout.splitlines() + untracked.stdout.splitlines()) - {CHART_FILE})
    if not changed:
        return f"chart files unchanged since {base_ref}"
    base_chart = git(root, "show", f"{base}:{CHART_FILE}")
    if base_chart.returncode != 0:
        return f"{CHART_DIR} is new since {base_ref}"
    before = chart_version(base_chart.stdout)
    after = chart_version((root / CHART_FILE).read_text(encoding="utf-8"))
    require(
        before != after,
        f"{CHART_FILE} version {after}를 올려야 함 ({base_ref} 이후 변경: {', '.join(changed)})",
    )
    return f"chart version {before} -> {after} covers {len(changed)} changed chart files"


# base ref가 있는지는 validate.sh가 확인하고, 없으면 건너뛴 검사로 기록한다.
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-ref", help="chart version bump를 비교할 Git ref")
    args = parser.parse_args()
    inside = git(ROOT, "rev-parse", "--is-inside-work-tree")
    require(inside.returncode == 0, "Git 작업 트리에서 실행해야 함")
    count = validate_argocd_tree(ROOT)
    print(f"root Application source holds only Kubernetes manifests ({count} files) outside its excludes")
    if args.base_ref:
        print(validate_chart_version_bump(ROOT, args.base_ref))


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        sys.exit(f"GitOps source validation failed: {error}")

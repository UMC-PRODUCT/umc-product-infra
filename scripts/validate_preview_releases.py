#!/usr/bin/env python3
"""Preview 성공 기록과 ApplicationSet의 배포 조건을 검증한다."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
RELEASE_DIRECTORY = Path("argocd/preview-releases")
RELEASE_PATTERN = "argocd/preview-releases/pr-{{ .number }}.json"
INFRA_REPOSITORY = "https://github.com/UMC-PRODUCT/umc-product-infra.git"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def unique_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        require(key not in result, "Preview JSON에 중복 key가 있음")
        result[key] = value
    return result


def validate_release(path: Path) -> dict:
    require(not path.is_symlink(), "Preview 기록은 symlink일 수 없음")
    match = re.fullmatch(r"pr-([1-9][0-9]*)\.json", path.name)
    require(match is not None, "Preview 기록 파일명이 잘못됨")
    record = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    require(isinstance(record, dict) and set(record) == {"release"}, "release object만 허용")
    release = record["release"]
    require(
        isinstance(release, dict)
        and set(release) == {"prNumber", "headSha", "imageTag", "imageDigest"}
        and all(isinstance(value, str) for value in release.values()),
        "Preview release schema 불일치",
    )
    require(release["prNumber"] == match[1], "파일명과 PR 번호 불일치")
    require(re.fullmatch(r"[a-f0-9]{40}", release["headSha"]) is not None, "잘못된 head SHA")
    require(release["imageTag"] == release["headSha"][:12], "tag와 head SHA 불일치")
    require(re.fullmatch(r"sha256:[a-f0-9]{64}", release["imageDigest"]) is not None, "잘못된 digest")
    return record


def validate_handoff(appset: dict) -> None:
    spec = appset["spec"]
    # 실패한 빌드의 head를 필터로 쓰면 기존 Application이 삭제될 수 있어 구조 자체를 고정한다.
    require(spec["generators"] == [{"matrix": {"generators": [
        {"pullRequest": {
            "github": {
                "owner": "UMC-PRODUCT", "repo": "umc-product-server",
                "tokenRef": {"secretName": "preview-github-token", "key": "token"},
                "labels": ["preview"],
            },
            "filters": [{"targetBranchMatch": "^develop$"}],
            "requeueAfterSeconds": 120,
        }},
        {"git": {
            "repoURL": INFRA_REPOSITORY, "revision": "main", "requeueAfterSeconds": 120,
            "files": [{"path": RELEASE_PATTERN}],
        }},
    ]}}], "Preview는 열린 PR과 그 PR의 마지막 성공 기록만 조합해야 함")
    template = spec["template"]
    parameters = {item["name"]: item for item in template["spec"]["source"]["helm"]["parameters"]}
    for name, field in (("image.tag", "imageTag"), ("image.digest", "imageDigest")):
        require(parameters.get(name) == {
            "name": name, "value": "{{ .release." + field + " }}", "forceString": True,
        }, "Preview 이미지는 성공 기록에서 문자열 tag/digest로만 가져와야 함")
    require(".head_sha" not in yaml.safe_dump(template["spec"]), "최신 PR head가 배포 spec에 들어감")
    require(
        template["metadata"]["annotations"]["infra.university.neordinary.com/preview-deployed-sha"]
        == "{{ .release.headSha }}", "실제 배포 SHA를 별도 표시해야 함",
    )
    require(spec["syncPolicy"] == {"preserveResourcesOnDeletion": False}, "PR 삭제 정책 변경")
    require(template["metadata"]["finalizers"] == ["resources-finalizer.argocd.argoproj.io"], "삭제 finalizer 누락")


def main() -> None:
    appset = yaml.safe_load((ROOT / "argocd/applications/preview/applicationset.yaml").read_text())
    validate_handoff(appset)
    directory = ROOT / RELEASE_DIRECTORY
    require(directory.is_dir() and not directory.is_symlink(), "성공 기록 디렉터리가 필요함")
    count = 0
    for path in directory.iterdir():
        if path.name == ".gitkeep":
            continue
        validate_release(path)
        count += 1
    print(f"Preview handoff contract and {count} successful release records validated")


if __name__ == "__main__":
    main()

"""Keep pinned chart rendering available when GitHub release downloads fail."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request


def download_release_asset(url: str, destination: Path) -> None:
    match = re.fullmatch(
        r"https://github\.com/([^/]+/[^/]+)/releases/download/([^/]+)/([^/]+)", url,
    )
    if match is None:
        raise ValueError(f"not a GitHub release asset URL: {url}")
    repository, tag, filename = match.groups()
    print(f"Retrying release download via GitHub API: {url}", file=sys.stderr)
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/releases/tags/{tag}",
        headers={"User-Agent": "umc-infra-validator"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        release = json.load(response)
    asset = next(item for item in release["assets"]
                 if item["name"] == urllib.parse.unquote(filename))
    request = urllib.request.Request(
        asset["url"],
        headers={"User-Agent": "umc-infra-validator", "Accept": "application/octet-stream"},
    )
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as stream:
        shutil.copyfileobj(response, stream)
    if asset.get("digest") and asset["digest"] != "sha256:" + hashlib.sha256(destination.read_bytes()).hexdigest():
        raise ValueError(f"release asset checksum mismatch: {filename}")


def template_chart(command: list[str], values: str) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(command, input=values, text=True, capture_output=True, check=False)
    if completed.returncode == 0:
        return completed
    # Helm이 선택한 고정 버전의 URL만 우회한다. values·템플릿 오류는 원래 실패를 유지한다.
    match = re.search(
        r"failed to fetch (https://github\.com/\S+/releases/download/\S+) : (?:500|502|503|504)\b",
        completed.stderr,
    )
    if match is None:
        return completed
    with tempfile.TemporaryDirectory(prefix="umc-chart-") as directory:
        archive = Path(directory) / "chart.tgz"
        download_release_asset(match[1], archive)
        local_command = command.copy()
        local_command[3] = str(archive)
        for option in ("--repo", "--version"):
            index = local_command.index(option)
            del local_command[index:index + 2]
        return subprocess.run(local_command, input=values, text=True, capture_output=True, check=False)

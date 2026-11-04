#!/usr/bin/env bash
# validate.sh가 source한다. 도구나 비교 기준이 없어 건너뛴 검사를 모아 최종 결과에 남긴다.
skipped_checks=()

# 전제 명령이 실패하면 검사를 건너뛴 것으로 기록하고 1을 반환한다.
# 예: 도구가 없거나(command -v), Docker CLI만 있고 daemon이 꺼져 있으면(docker info) 검사를 돌릴 수 없다.
optional_check() {
  local check="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    return 0
  fi
  skipped_checks+=("$check")
  echo "$check skipped: '$*' failed"
  return 1
}

# scripts/requirements-lint.txt에 고정한 버전이 python3 환경에 그대로 있을 때만 0을 반환한다.
# 버전이 다른 ruff·mypy는 판정이 달라질 수 있어 설치되지 않은 것과 같이 건너뛴다.
pinned_python_tools() {
  python3 - "$@" <<'PY'
import importlib.metadata
import re
import sys
from pathlib import Path

lock = Path("scripts/requirements-lint.txt").read_text(encoding="utf-8")
pins = dict(re.findall(r"^([a-z0-9-]+)==(\S+)", lock, re.MULTILINE))
for name in sys.argv[1:]:
    try:
        installed = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        sys.exit(1)
    if installed != pins.get(name):
        sys.exit(1)
PY
}

# 건너뛴 검사를 성공 문구 안에 드러내고, VALIDATE_STRICT=1이면 하나라도 있을 때 실패한다.
finish_validation() {
  if ((${#skipped_checks[@]} == 0)); then
    echo "umc-infra validation passed"
    return 0
  fi
  local summary
  summary="$(printf '%s, ' "${skipped_checks[@]}")"
  summary="${summary%, }"
  if [[ "${VALIDATE_STRICT:-}" == 1 ]]; then
    echo "umc-infra validation incomplete: VALIDATE_STRICT=1 forbids skipped checks (skipped: $summary)" >&2
    return 1
  fi
  echo "umc-infra validation passed (skipped: $summary)"
}

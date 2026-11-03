#!/usr/bin/env bash
# validate.sh가 source한다. 도구가 없어 건너뛴 검사를 모아 최종 결과에 남긴다.
skipped_checks=()

# 도구가 없거나 뒤따르는 확인 명령이 실패하면 건너뛴 검사로 기록하고 1을 반환한다.
# 예: Docker CLI만 있고 daemon이 꺼져 있으면 검사를 돌릴 수 없다.
tool_available() {
  local tool="$1" check="$2" reason="not installed"
  shift 2
  if command -v "$tool" >/dev/null; then
    if (($# == 0)) || "$@" >/dev/null 2>&1; then
      return 0
    fi
    reason="not usable ($*)"
  fi
  skipped_checks+=("$check")
  echo "$tool $reason; $check skipped"
  return 1
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

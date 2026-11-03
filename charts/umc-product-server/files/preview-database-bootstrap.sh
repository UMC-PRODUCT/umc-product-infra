#!/usr/bin/env bash
# PR Preview database를 준비한다. createdb Job이 이 파일을 그대로 실행한다.
#
# COPY_FROM_DEV=false: 빈 database와 확장만 만든다. 이미 있으면 그대로 둔다.
# COPY_FROM_DEV=true : dev DB를 덤프해 PR database로 복원하고 외부 실행 상태를 정리한 뒤
#                      database 주석에 완료 표시를 남긴다.
#   - 완료 표시가 있으면 아무것도 하지 않는다. 새 커밋 배포·재시도가 QA 데이터를 덮어쓰지 않는다.
#   - 완료 표시가 없는 database는 중간에 실패한 복원이므로 지우고 처음부터 다시 만든다.
#
# 관리자 연결은 PGHOST·PGUSER·PGPASSWORD·PGDATABASE(preview 공용 PostgreSQL)를 쓰고,
# dev 연결은 SOURCE_* 값을 명령별 환경변수로만 넘긴다. 비밀값을 명령행 인자에 두지 않는다.
set -euo pipefail

readonly MARKER_PREFIX="umc-preview-restore-complete"

reject() {
  echo "database 준비 거부: $1" >&2
  exit 64
}

wait_for() {
  # NetworkPolicy가 새 Pod IP를 허용하기까지 몇 초 걸리므로 짧게 재시도한다.
  # 두 번째 인자는 3초 간격 시도 횟수다.
  for _ in $(seq 1 "$2"); do
    if pg_isready -q -h "$1"; then
      return 0
    fi
    sleep 3
  done
  echo "PostgreSQL 연결 대기 실패: $1" >&2
  return 1
}

admin_psql() {
  psql -X -v ON_ERROR_STOP=1 "$@"
}

source_psql() {
  PGPASSWORD="$SOURCE_PASSWORD" psql -X -v ON_ERROR_STOP=1 \
    --host "$SOURCE_HOST" --username "$SOURCE_USER" --dbname "$SOURCE_DB" "$@"
}

if [ "$TARGET_OWNER" != "umc_product_preview_app" ]; then
  reject "허용되지 않은 owner: $TARGET_OWNER"
fi
case "$TARGET_DB" in
  umc_product_pr[0-9]*) ;;
  *) reject "허용되지 않은 이름: $TARGET_DB" ;;
esac
target_suffix="${TARGET_DB#umc_product_pr}"
case "$target_suffix" in
  "" | *[!0-9]*) reject "허용되지 않은 이름: $TARGET_DB" ;;
esac
case "$COPY_FROM_DEV" in
  true | false) ;;
  *) reject "COPY_FROM_DEV는 true 또는 false여야 한다" ;;
esac

# 공용 Preview DB는 preview-lowest라 노드 재시작 뒤 늦게 뜰 수 있다. startupProbe 상한(5분)보다 긴
# 10분을 기다린다. createdb 템플릿은 activeDeadlineSeconds가 이 대기와 복원 시간을 담는지 검사한다.
wait_for "$PGHOST" 200

state="$(admin_psql -tA --set=db="$TARGET_DB" --set=marker="${MARKER_PREFIX}%" <<'SQL'
SELECT CASE
  WHEN d.oid IS NULL THEN 'absent'
  WHEN shobj_description(d.oid, 'pg_database') LIKE :'marker' THEN 'complete'
  ELSE 'incomplete'
END
FROM (SELECT 1) AS one
LEFT JOIN pg_database AS d ON d.datname = :'db';
SQL
)"

if [ "$state" = "complete" ]; then
  echo "dev 복원이 이미 완료된 database다. 데이터를 유지한다: $TARGET_DB"
  exit 0
fi

if [ "$COPY_FROM_DEV" = "true" ] && [ "$state" = "incomplete" ]; then
  echo "완료 표시가 없는 database를 지우고 다시 복원한다: $TARGET_DB"
  admin_psql --set=db="$TARGET_DB" <<'SQL'
DROP DATABASE IF EXISTS :"db" WITH (FORCE);
SQL
fi

# \gexec는 SELECT가 만든 SQL을 실행한다. CREATE DATABASE는 IF NOT EXISTS와 트랜잭션을 지원하지 않는다.
admin_psql --set=db="$TARGET_DB" --set=owner="$TARGET_OWNER" <<'SQL'
SELECT format('CREATE DATABASE %I OWNER %I', :'db', :'owner')
  WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'db') \gexec
SQL
# Flyway와 복원은 최소권한 app role로 실행되므로 관리자 자격증명으로 확장을 먼저 설치한다.
admin_psql --dbname "$TARGET_DB" <<'SQL'
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS btree_gist;
SQL

if [ "$COPY_FROM_DEV" != "true" ]; then
  echo "database 준비 완료: $TARGET_DB"
  exit 0
fi

wait_for "$SOURCE_HOST" 40

# dev에 추가된 확장도 같은 이름으로 먼저 설치한다. 덤프에서는 확장을 제외한다.
source_psql -tA -c "SELECT extname FROM pg_extension WHERE extname <> 'plpgsql' ORDER BY extname" |
  while read -r extension; do
    admin_psql --dbname "$TARGET_DB" --set=extension="$extension" <<'SQL'
SELECT format('CREATE EXTENSION IF NOT EXISTS %I', :'extension') \gexec
SQL
  done

# 확장이 만든 스키마(postgis_tiger_geocoder의 tiger 등)는 위 설치 때 이미 생겼다.
# --exclude-extension은 확장 객체만 빼고 이 스키마의 CREATE SCHEMA는 남기므로 덤프에서 뺀다.
# 새 database라 public 밖의 스키마는 확장이 만든 것뿐이다.
extension_schemas="$(admin_psql --dbname "$TARGET_DB" -tA -c \
  "SELECT nspname FROM pg_namespace WHERE nspname NOT IN ('public', 'information_schema') AND nspname !~ '^pg_' ORDER BY nspname")"
exclude_schema_args=()
while read -r schema; do
  if [ -n "$schema" ]; then
    exclude_schema_args+=("--exclude-schema=\"$schema\"")
  fi
done <<< "$extension_schemas"

# pg_dump는 한 snapshot에서 일관되게 읽는다. 원본 owner·권한·주석은 재생하지 않고
# app role로 객체를 만든다. Flyway 이력과 checksum은 테이블 데이터로 그대로 복사된다.
# 덤프 파일을 남기지 않도록 바로 흘려 넣는다. 중간 실패는 완료 표시가 없어 다음 실행이 다시 만든다.
echo "dev 덤프를 복원한다: $SOURCE_DB -> $TARGET_DB"
{
  printf 'SET ROLE %s;\n' "$TARGET_OWNER"
  PGPASSWORD="$SOURCE_PASSWORD" PGOPTIONS="" pg_dump \
    --host "$SOURCE_HOST" --username "$SOURCE_USER" --dbname "$SOURCE_DB" \
    --format=plain --no-owner --no-privileges --no-comments \
    --no-publications --no-subscriptions --no-security-labels \
    --exclude-extension='*' "${exclude_schema_args[@]}"
} | PGOPTIONS="-c statement_timeout=0" admin_psql --dbname "$TARGET_DB" --single-transaction --quiet --output=/dev/null

# 로그인 세션·인증 코드·대기 중인 발송 같은 외부 실행 상태만 지운다. 회원·업무 데이터는 유지한다.
{
  printf 'SET ROLE %s;\n' "$TARGET_OWNER"
  printf '%s\n' "$CLEANUP_SQL"
} | admin_psql --dbname "$TARGET_DB" --single-transaction --quiet --output=/dev/null

completed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
admin_psql --set=db="$TARGET_DB" --set=marker="${MARKER_PREFIX} ${completed_at} source=${SOURCE_DB}" <<'SQL'
COMMENT ON DATABASE :"db" IS :'marker';
SQL
echo "dev 복원 완료: $TARGET_DB"

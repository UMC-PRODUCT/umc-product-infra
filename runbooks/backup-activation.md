# PostgreSQL backup 활성화

목표는 S3 upload가 아니라 실제 restore 가능성을 확인하는 것이다.
CronJob은 이 절차가 끝날 때까지 `suspend: true`로 유지한다.
검증은 prod node 밖에서 운영 DB와 같은 PostgreSQL 18·PostGIS 3.6 image digest로 수행한다.

## 전제

- `umc-product-postgres-backup-s3` stack이 실제 bucket으로 배포됐다.
- `/umc-product/prod/backup-s3` ExternalSecret이 새 version으로 Ready다.
- cluster writer는 `postgres/prod/*` write만 할 수 있다.
- 운영자는 별도 read-only AWS identity를 사용한다.
- read identity는 대상 bucket의 `postgres/prod/*` 조회에 필요한 ListBucket과
  GetObject/GetObjectVersion만 가진다. cluster의 write-only credential에 읽기 권한을 추가하지 않는다.
- 로컬에 AWS CLI, Docker, `sha256sum`이 있다.
- 개인정보가 포함된 backup을 내려받을 수 있는 승인된 로컬 환경과 충분한 여유 공간이 있다.

writer와 read identity를 같은 credential로 사용하지 않는다.
Secret 값을 셸 인자, Git, log에 출력하지 않는다.
복원 대상은 아래에서 새로 만드는 격리 Docker DB뿐이다. 운영 DB에는 복원 명령을 실행하지 않는다.

## 1. 계약 확인

```bash
kubectl get cronjob/pg-backup -n db \
  -o custom-columns='NAME:.metadata.name,SUSPEND:.spec.suspend,SCHEDULE:.spec.schedule,TZ:.spec.timeZone'
kubectl wait --for=condition=Ready externalsecret/backup-s3 -n db --timeout=180s
```

`SUSPEND`는 `true`여야 한다.
수동 `create job --from=cronjob`은 suspended CronJob에서도 실행된다.

## 2. 수동 backup

```bash
set -euo pipefail

active="$(kubectl get jobs -n db \
  -l app.kubernetes.io/name=pg-backup \
  -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.active}{"\n"}{end}' \
  | awk '$2 + 0 > 0 {print $1}')"
test -z "$active"

backup_started_epoch="$(date +%s)"
backup_job="pg-backup-activation-$(date -u +%Y%m%d%H%M%S)"
kubectl create job -n db --from=cronjob/pg-backup "$backup_job"
kubectl wait --for=condition=Complete "job/$backup_job" -n db --timeout=7200s
backup_finished_epoch="$(date +%s)"

backup_pod_uid="$(kubectl get pod -n db -l "job-name=$backup_job" \
  -o jsonpath='{.items[?(@.status.phase=="Succeeded")].metadata.uid}')"
case "$backup_pod_uid" in
  ????????-????-????-????-????????????) ;;
  *) exit 1 ;;
esac

kubectl logs -n db "job/$backup_job" -c dump
kubectl logs -n db "job/$backup_job" -c upload
```

Job이 실패하면 `suspend:true`를 유지한다.
deadline을 늘리기 전에 DB 크기, nodefs, dump 시간, upload 시간을 기록한다.

## 3. S3 object 검증

다음 환경변수는 승인된 read identity의 환경에서 설정한다.
값을 이 저장소에 기록하지 않는다.

```bash
BACKUP_BUCKET="${BACKUP_BUCKET:?backup bucket 이름을 설정하세요}"
BACKUP_REGION="${BACKUP_REGION:?backup region을 설정하세요}"
BACKUP_OWNER="${BACKUP_OWNER:?12자리 AWS account ID를 설정하세요}"
```

성공한 Pod UID와 같은 marker 하나만 선택한다.

```bash
umask 077
restore_dir="$(mktemp -d "${TMPDIR:-/tmp}/umc-backup-restore.XXXXXX")"
restore_container="umc-product-restore-verify-${backup_pod_uid}"
restore_volume="${restore_container}-data"
# 실패 원인을 조사할 수 있도록 EXIT trap으로 dump·log·volume을 자동 삭제하지 않는다.
printf 'restore artifacts: %s\n' "$restore_dir"

marker_key="$(aws s3api list-objects-v2 \
  --bucket "$BACKUP_BUCKET" \
  --prefix postgres/prod/ \
  --expected-bucket-owner "$BACKUP_OWNER" \
  --region "$BACKUP_REGION" \
  --query "Contents[?ends_with(Key, \`-${backup_pod_uid}.dump.complete\`)].Key | [0]" \
  --output text)"
case "$marker_key" in
  postgres/prod/umc-product-????????T??????Z-"$backup_pod_uid".dump.complete) ;;
  *) exit 1 ;;
esac

aws s3api get-object \
  --bucket "$BACKUP_BUCKET" --key "$marker_key" \
  --expected-bucket-owner "$BACKUP_OWNER" --region "$BACKUP_REGION" \
  --checksum-mode ENABLED "$restore_dir/marker" >/dev/null
backup_name="$(cat "$restore_dir/marker")"
case "$backup_name" in
  umc-product-????????T??????Z-"$backup_pod_uid".dump) ;;
  *) exit 1 ;;
esac

for suffix in "" .sha256; do
  aws s3api get-object \
    --bucket "$BACKUP_BUCKET" --key "postgres/prod/$backup_name$suffix" \
    --expected-bucket-owner "$BACKUP_OWNER" --region "$BACKUP_REGION" \
    --checksum-mode ENABLED "$restore_dir/$backup_name$suffix" >/dev/null
done

(
  cd "$restore_dir"
  sha256sum -c "$backup_name.sha256"
)
```

세 object는 dump, checksum, complete marker다.
marker 없는 dump는 완료된 backup으로 취급하지 않는다.
remote object의 server-side encryption이 AES256인지도 확인한다.

```bash
for key in \
  "postgres/prod/$backup_name" \
  "postgres/prod/$backup_name.sha256" \
  "$marker_key"; do
  test "$(aws s3api head-object \
    --bucket "$BACKUP_BUCKET" --key "$key" \
    --expected-bucket-owner "$BACKUP_OWNER" --region "$BACKUP_REGION" \
    --query ServerSideEncryption --output text)" = AES256
done
```

## 4. node 밖 restore

검증 container는 network를 끄고 새 전용 volume만 사용하며 host port를 열지 않는다.
image digest는 cluster PostgreSQL과 같다. Apple Silicon에서도 운영과 같은 `linux/amd64`를 사용한다.
`template0`으로 새 DB를 만들고 extension은 미리 설치하지 않는다. 전체 dump가 직접 생성한다.

```bash
postgres_image='postgis/postgis:18-3.6@sha256:60f6ad1d21ea86a67d47780b9a0d1e1d200500f62b19293fa834d0dea80b8677'
restore_started_epoch="$(date +%s)"
if docker container inspect "$restore_container" >/dev/null 2>&1 \
  || docker volume inspect "$restore_volume" >/dev/null 2>&1; then
  echo "restore container/volume이 이미 존재합니다. 재사용하지 말고 이전 검증 결과를 확인하세요." >&2
  exit 1
fi
docker volume create "$restore_volume" >/dev/null
docker run -d --name "$restore_container" --platform linux/amd64 --network none \
  --mount "type=volume,source=$restore_volume,target=/var/lib/postgresql" \
  -e POSTGRES_HOST_AUTH_METHOD=trust \
  "$postgres_image"

attempts=0
# entrypoint의 초기화용 임시 server는 TCP를 열지 않는다. 초기화가 끝난 server를 기다린다.
until docker exec "$restore_container" pg_isready -h 127.0.0.1 -U postgres; do
  attempts=$((attempts + 1))
  test "$attempts" -lt 60 || exit 1
  sleep 2
done

docker exec -i "$restore_container" pg_restore --list \
  < "$restore_dir/$backup_name" > "$restore_dir/restore.toc"
```

`restore.toc`에서 소유자를 확인한다. prod 계약의 소유자는 `prod_postgres_admin`과
`umc_product_app`이다. 다른 소유자가 있으면 먼저 원인을 확인하고 아래 절차를 재검토한다.
비밀번호·운영 권한을 복사하지 않고 복원용 NOLOGIN 역할만 만든다.

```bash
docker exec -i "$restore_container" psql -X -v ON_ERROR_STOP=1 -U postgres -d postgres <<'SQL'
CREATE ROLE prod_postgres_admin NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
CREATE ROLE umc_product_app NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
SQL
docker exec "$restore_container" createdb -U postgres --template=template0 \
  -O umc_product_app umc_product_restore_verify
docker exec -i "$restore_container" psql -X -v ON_ERROR_STOP=1 -U postgres \
  -d umc_product_restore_verify <<'SQL'
DO $guard$
BEGIN
  IF current_database() <> 'umc_product_restore_verify'
    OR EXISTS (
      SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema'
    )
    OR EXISTS (SELECT 1 FROM pg_extension WHERE extname <> 'plpgsql')
  THEN
    RAISE EXCEPTION '새 격리 검증 DB가 아니므로 복원을 중단합니다';
  END IF;
END;
$guard$;
-- dump에 CREATE SCHEMA public이 포함된다. 이 빈 검증 DB에서만 제거하며 CASCADE는 쓰지 않는다.
DROP SCHEMA public;
SQL
docker exec -i "$restore_container" pg_restore \
  --exit-on-error --single-transaction --no-acl \
  -U postgres -d umc_product_restore_verify \
  < "$restore_dir/$backup_name" > "$restore_dir/restore.log" 2>&1

restored_relations="$(docker exec "$restore_container" psql -At -U postgres \
  -d umc_product_restore_verify \
  -c "select count(*) from pg_class where relkind in ('r','p') and relnamespace = 'public'::regnamespace and relowner = 'umc_product_app'::regrole")"
test "$restored_relations" -gt 0
docker exec -i "$restore_container" psql -X -v ON_ERROR_STOP=1 -U postgres \
  -d umc_product_restore_verify <<'SQL'
SELECT extname, extversion FROM pg_extension ORDER BY extname;
SELECT count(*) AS members FROM public.member;
SELECT count(*) AS challengers FROM public.challenger;
SELECT count(*) AS migrations, count(*) FILTER (WHERE NOT success) AS failed_migrations
FROM public.flyway_schema_history;
SQL
restore_finished_epoch="$(date +%s)"
```

전체 restore가 오류 없이 끝나고 app table·sequence 소유자, extension 버전,
Flyway 이력과 대표 row count가 backup 시점에 기대한 값인지 확인한다. 운영 DB는 계속 쓰기가
발생하므로 나중에 조회한 운영 row count가 backup과 반드시 같지는 않다. 개인정보는 출력하지 않는다.
오류 log에는 데이터가 들어갈 수 있으므로 내용을 그대로 공유하지 않는다.

전체 dump에는 관리자 소유 extension과 schema도 있으므로 app 역할로 복원하지 않는다.
검증 container의 `postgres`로 실행하되 `--no-owner`를 쓰지 않아 app 객체 소유자는 복구한다.
`--no-acl`로 운영 GRANT/REVOKE는 가져오지 않으며 extension 내부 객체 소유자는 달라질 수 있다.
이 검사는 데이터 복구 가능성 검증이다. 실제 운영 복구 시에는 Git의 role Job으로 접근 권한을
별도 검증한다. `--clean`·`--create`로 기존 DB를 덮어쓰지 않는다.

## 5. RPO와 RTO 기록

```bash
backup_elapsed_seconds="$((backup_finished_epoch - backup_started_epoch))"
restore_elapsed_seconds="$((restore_finished_epoch - restore_started_epoch))"
marker_last_modified="$(aws s3api head-object \
  --bucket "$BACKUP_BUCKET" --key "$marker_key" \
  --expected-bucket-owner "$BACKUP_OWNER" --region "$BACKUP_REGION" \
  --query LastModified --output text)"
printf 'backup=%s\nmarker=%s\nbackup_seconds=%s\nrestore_test_seconds=%s\nrelations=%s\n' \
  "$backup_name" "$marker_last_modified" "$backup_elapsed_seconds" \
  "$restore_elapsed_seconds" "$restored_relations"
```

운영 기록에 backup source time, marker time, restore 완료 time, dump 크기, RPO, RTO를 남긴다.
RPO는 마지막 복구 가능 backup과 장애 가정 시점의 차이다.
위 `restore_test_seconds`는 DB restore 구성요소만 측정한다.
전체 RTO는 실제 재해 복구 훈련에서 대체 node 준비, cluster 수렴, DB restore를 합쳐 별도 기록한다.

## 6. 예약 실행 활성화

앞 단계가 모두 성공한 PR에서만 다음 한 줄을 바꾼다.

```yaml
spec:
  suspend: false
```

대상은 `manifests/postgres/prod/backup-cronjob.yaml`이다.
`Static validation`을 통과한 PR로 merge한다.
live patch만 남기지 않는다.

첫 03:00 실행 뒤 새 marker와 외부 restore를 다시 확인한다.
`PostgresBackupFailed`, `PostgresBackupNeverSucceeded`, `PostgresBackupStale` 규칙이 로드됐는지 확인한다.
stale 경보는 **CronJob의 마지막 정규 성공 시각**이 30시간을 넘고 10분간 유지되면 발생한다.
활성화 후 정규 성공 시각 자체가 없으면 27시간 뒤 별도 경보가 발생한다.
수동 `create job --from=cronjob` 성공은 CronJob의 `lastSuccessfulTime`을 채우지 않는다.
업로더는 S3 complete marker까지 성공해야 종료하지만, 이 경보는 S3 object를 직접 읽지 않는다.
이후 object 삭제·손상이나 모니터링을 포함한 전체 노드 장애를 독립 감지하는 장치는 아니다.
dump와 upload p95의 두 배를 `activeDeadlineSeconds` 기준으로 사용한다.

실패하면 `suspend: true`로 되돌리는 PR을 즉시 merge한다.
기존 S3 object는 삭제하지 않는다.
active Job 삭제는 data corruption이나 credential incident가 아니면 피한다.

## 7. 검증 자료 정리

성공 결과를 운영 기록에 남긴 뒤에만 검증용 자원을 직접 정리한다. 실패했다면 먼저 원인을 조사한다.

```bash
printf 'container=%s\nvolume=%s\nartifacts=%s\n' "$restore_container" "$restore_volume" "$restore_dir"
docker container inspect "$restore_container" --format '{{.Name}} {{json .Mounts}}'
docker volume inspect "$restore_volume" --format '{{.Name}}'
```

출력된 이름과 mount가 이번에 만든 검증 전용 자원인지 확인하고, 그 정확한 이름의 container를
중지·삭제한 뒤 해당 volume만 삭제한다. 내려받은 dump·checksum·log는 더 이상 필요 없을 때
출력된 디렉터리를 로컬 휴지통으로 옮긴다. 광범위한 Docker prune이나 변수 기반 재귀 삭제는
사용하지 않으며 운영 PVC·S3 backup object는 삭제하지 않는다.

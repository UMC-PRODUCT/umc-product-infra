# PostgreSQL backup 활성화

목표는 S3 upload가 아니라 실제 restore 가능성을 확인하는 것이다.
CronJob은 이 절차가 끝날 때까지 `suspend: true`로 유지한다.
검증은 prod node 밖에서 운영 DB와 같은 PostgreSQL 18·PostGIS 3.6 image digest로 수행한다.

## 백업 정책

- 대상은 prod DB이며 매일 03:00 `Asia/Seoul`에 전체 논리 백업을 만든다.
- S3 현재 버전은 생성 후 **10일**에 만료한다. 성공한 백업 **10개**를 보장하는 정책은 아니다.
  하루 한 번 성공하면 대략 10회분이며, 수동 백업도 같은 보관 기간을 적용한다.
- Versioning 때문에 만료된 원본은 비현행 버전이 된 뒤 **1일**을 기준으로 영구 삭제된다.
  정리는 비동기이므로 모든 객체가 정확히 240시간 만에 사라지지는 않는다.
- 대기 서버와 시점 복구(PITR)는 운영하지 않는다. 정상적인 일일 백업 기준 최대 약 하루의
  데이터 손실을 감수하며, 장애 시 서버 재구성과 마지막 정상 백업 복원을 수행한다.
  백업 실패가 이어지면 손실 범위가 늘어나므로 실패·지연 경보에 대응해야 한다.
- 임시 복원 검증용 Docker DB는 대기 서버가 아니며 검증 뒤 정리한다.

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

1~2단계는 개인 관리자 공인 SSH로 접속한 IDC의 같은 Bash 세션에서 실행한다.
클러스터 명령에는 `sudo k3s kubectl`을 사용하고 kubeconfig를 PC로 복사하지 않는다.

```bash
sudo k3s kubectl get cronjob/pg-backup -n db \
  -o custom-columns='NAME:.metadata.name,SUSPEND:.spec.suspend,SCHEDULE:.spec.schedule,TZ:.spec.timeZone'
sudo k3s kubectl wait --for=condition=Ready externalsecret/backup-s3 -n db --timeout=180s
```

`SUSPEND`는 `true`여야 한다.
수동 `create job --from=cronjob`은 suspended CronJob에서도 실행된다.

## 2. 수동 backup

```bash
set -euo pipefail

active="$(sudo k3s kubectl get jobs -n db \
  -l app.kubernetes.io/name=pg-backup \
  -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.active}{"\n"}{end}' \
  | awk '$2 + 0 > 0 {print $1}')"
test -z "$active"

backup_started_epoch="$(date +%s)"
backup_job="pg-backup-activation-$(date -u +%Y%m%d%H%M%S)"
sudo k3s kubectl create job -n db --from=cronjob/pg-backup "$backup_job"
sudo k3s kubectl wait --for=condition=Complete "job/$backup_job" -n db --timeout=7200s
backup_finished_epoch="$(date +%s)"

backup_pod_uid="$(sudo k3s kubectl get pod -n db -l "job-name=$backup_job" \
  -o jsonpath='{.items[?(@.status.phase=="Succeeded")].metadata.uid}')"
case "$backup_pod_uid" in
  ????????-????-????-????-????????????) ;;
  *) exit 1 ;;
esac

sudo k3s kubectl logs -n db "job/$backup_job" -c dump
sudo k3s kubectl logs -n db "job/$backup_job" -c upload
printf 'backup_pod_uid=%s\nbackup_started_epoch=%s\nbackup_finished_epoch=%s\n' \
  "$backup_pod_uid" "$backup_started_epoch" "$backup_finished_epoch"
```

Job이 실패하면 `suspend:true`를 유지한다.
deadline을 늘리기 전에 DB 크기, nodefs, dump 시간, upload 시간을 기록한다.

## 3. S3 object 검증

3~5단계는 prod node 밖의 로컬 Bash 세션에서 실행한다. 2단계에서 출력한 Pod UID와 시작·완료
시각을 같은 이름의 로컬 변수로 설정하고, 별도의 승인된 read identity를 사용한다.
Secret이나 kubeconfig는 전달하지 않으며 아래 입력값을 이 저장소에 기록하지 않는다.

```bash
set -euo pipefail

backup_pod_uid="${backup_pod_uid:?2단계의 성공한 Pod UID를 설정하세요}"
backup_started_epoch="${backup_started_epoch:?2단계의 backup 시작 시각을 설정하세요}"
backup_finished_epoch="${backup_finished_epoch:?2단계의 backup 완료 시각을 설정하세요}"
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

`--no-acl`은 dump에 포함된 grant/revoke를 복원하지 않는다. 위 데이터 복원만으로 권한 검증이
끝난 것은 아니다. 검증 DB에 필요한 role과 grant를 Git의 role Job 계약에 맞춰 별도로 재현하고,
앱 owner·권한과 조회 전용 role의 읽기 성공·쓰기 거부를 확인한다. extension 소유권도 대조하고
복원 중 오류가 발생하면 예약 실행을 활성화하지 않는다.

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

## 6. S3 보관 정책 반영

Git merge와 Argo CD는 AWS CloudFormation을 자동 배포하지 않는다.
[백업 S3 template](../../cloud/aws/postgres-backup-s3.yaml)을 기존
`umc-product-postgres-backup-s3` stack에 별도로 반영한다.

1. 위 복원 검증을 먼저 완료하고, 현재 object·version 목록과 마지막 정상 백업을 확인한다.
2. 기존 bucket 이름을 유지한 change set을 검토한다. bucket 교체나 writer 권한 확대가 있으면 중단한다.
3. 변경을 적용하고 stack 완료와 실제 lifecycle을 확인한다. `postgres/prod/`의 현재 버전 만료
   10일, 비현행 버전 1일, 별도 expired delete marker 정리 규칙이 기준이다.

기간 축소는 **이미 존재하는 오래된 백업에도 적용**된다. 영구 삭제된 버전은 보관 기간을 다시
늘려도 복구할 수 없다. `DeletionPolicy: Retain`과 CronJob의 `suspend`는 lifecycle 삭제를 막지 않는다.

## 7. 예약 실행 활성화

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

## 8. 검증 자료 정리

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

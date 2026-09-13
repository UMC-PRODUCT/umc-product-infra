# PostgreSQL backup 활성화

목표는 S3 upload가 아니라 실제 restore 가능성을 확인하는 것이다.
CronJob은 이 절차가 끝날 때까지 `suspend: true`로 유지한다.
검증은 prod node 밖에서 클러스터와 동일한 고정 PostgreSQL/PostGIS image로 수행한다.

## 전제

- `umc-product-postgres-backup-s3` stack이 실제 bucket으로 배포됐다.
- `/umc-product/prod/backup-s3` ExternalSecret이 새 version으로 Ready다.
- cluster writer는 `postgres/prod/*` write만 할 수 있다.
- 운영자는 별도 read-only AWS identity를 사용한다.
- read identity는 ListBucket과 GetObject/GetObjectVersion만 가진다.
- 로컬에 AWS CLI, Docker, `sha256sum`이 있다.
- 현재 prod DB가 restore test에 사용해도 되는 상태다.

writer와 read identity를 같은 credential로 사용하지 않는다.
Secret 값을 셸 인자, Git, log에 출력하지 않는다.

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
restore_dir="$(mktemp -d)"
restore_container="umc-product-restore-verify-$(date -u +%Y%m%d%H%M%S)"
restore_volume="${restore_container}-data"
cleanup() {
  docker rm -f "$restore_container" >/dev/null 2>&1 || true
  docker volume rm "$restore_volume" >/dev/null 2>&1 || true
  rm -rf "$restore_dir"
}
trap cleanup EXIT INT TERM

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

검증 container는 network를 끄고 임시 filesystem만 사용한다.
image digest는 cluster PostgreSQL과 같다.

```bash
postgres_image='postgis/postgis:18-3.6@sha256:60f6ad1d21ea86a67d47780b9a0d1e1d200500f62b19293fa834d0dea80b8677'
restore_started_epoch="$(date +%s)"
docker volume create "$restore_volume" >/dev/null
docker run -d --name "$restore_container" --network none \
  --mount "type=volume,source=$restore_volume,target=/var/lib/postgresql" \
  -e POSTGRES_HOST_AUTH_METHOD=trust \
  "$postgres_image"

attempts=0
until docker exec "$restore_container" pg_isready -U postgres; do
  attempts=$((attempts + 1))
  test "$attempts" -lt 60 || exit 1
  sleep 2
done

docker exec "$restore_container" psql -v ON_ERROR_STOP=1 -U postgres -d postgres \
  -c 'CREATE ROLE umc_product_app NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION'
docker exec "$restore_container" createdb -U postgres -O umc_product_app umc_product_restore_verify
docker exec "$restore_container" psql -v ON_ERROR_STOP=1 -U postgres \
  -d umc_product_restore_verify \
  -c 'CREATE EXTENSION IF NOT EXISTS postgis; CREATE EXTENSION IF NOT EXISTS btree_gist;'
docker exec -i "$restore_container" pg_restore \
  --exit-on-error --no-owner --no-acl --role=umc_product_app \
  -U postgres -d umc_product_restore_verify \
  < "$restore_dir/$backup_name"

restored_relations="$(docker exec "$restore_container" psql -At -U postgres \
  -d umc_product_restore_verify \
  -c "select count(*) from pg_catalog.pg_class where relkind in ('r','p') and relnamespace not in (select oid from pg_catalog.pg_namespace where nspname like 'pg_%' or nspname = 'information_schema')")"
test "$restored_relations" -gt 0
restore_finished_epoch="$(date +%s)"
```

애플리케이션 핵심 table 수와 대표 row count를 추가로 확인한다.
개인정보 값은 결과 log에 출력하지 않는다.

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
30시간 stale alert가 정상인지 확인한다.
dump와 upload p95의 두 배를 `activeDeadlineSeconds` 기준으로 사용한다.

실패하면 `suspend: true`로 되돌리는 PR을 즉시 merge한다.
기존 S3 object는 삭제하지 않는다.
active Job 삭제는 data corruption이나 credential incident가 아니면 피한다.

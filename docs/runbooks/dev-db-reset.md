# dev DB 다시 만들기

dev `umc_product_dev`를 지우고 빈 DB로 다시 만든 뒤, Flyway와 QA 시딩으로 다시 채우는 절차다.
dev 데이터가 모두 사라지고 되돌릴 수 없다. prod·Preview DB에는 적용하지 않는다.

## 사용 시점

- 테스트 데이터가 쌓여 dev를 처음 상태로 돌리고 싶을 때
- DB locale을 ICU `ko-KR`로 바꿔야 할 때. `POSTGRES_INITDB_ARGS`는 빈 데이터 디렉터리의
  최초 initdb에만 적용되므로, 이미 있는 DB는 이 절차로 다시 만들어야 바뀐다.
- QA 계정 비밀번호를 바꿨을 때. 시딩은 이미 있는 계정을 건너뛰므로 다시 만들어야 새 값이 들어간다.

## 전제

- 실행자는 `umc-ssh-admins` 그룹으로 서버에 SSH 접속할 수 있다.

## 중단 조건

- 3단계 사전 확인에서 ICU `ko` collation이나 libc `C.UTF-8` locale이 없으면 멈춘다.
- 3단계에서 DROP은 됐는데 CREATE가 실패하면 앱을 재시작하지 않는다. 원인을 고친 뒤 CREATE만 다시 실행한다.
- 4단계 `rerun_role_jobs`가 실패하면 앱을 재시작하지 않는다. 확장과 앱 권한 없이
  Flyway가 돌면 첫 migration부터 실패한다.

## 절차

아래 명령은 서버에 SSH 접속한 뒤 실행한다.

### 1. 수동 권한 기록과 develop 머지 중지

팀 채널에 알리고 작업이 끝날 때까지 서버 레포 develop 머지를 멈춘다. 3~6단계 사이에 dev 배포
커밋이 들어오면 새 Pod가 4단계 Job보다 먼저 Flyway를 돌려 CrashLoop에 빠진다. 그렇게 됐다면
4단계부터 다시 한다.

DROP과 함께 DB 단위 권한과 설정도 사라진다. Git의 role Job은 `umc_product_dev_app`과
`umc_product_dev_preview_dump`의 권한만 다시 준다. 지우기 전에 현재 상태를 기록한다.

```bash
sudo k3s kubectl -n dev-db exec -i postgres-0 -- sh -c 'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d umc_product_dev' <<'SQL'
SELECT datname, datacl FROM pg_database WHERE datname = current_database();
\ddp
SELECT r.rolname, s.setconfig
FROM pg_db_role_setting s
JOIN pg_database d ON d.oid = s.setdatabase
LEFT JOIN pg_roles r ON r.oid = s.setrole
WHERE d.datname = current_database();
SQL
```

위 두 role 밖의 GRANT, default privilege, `ALTER DATABASE ... SET` 항목이 있으면 기록해 두고
마무리에서 다시 준다.

### 2. (선택) 백업

지운 데이터가 필요할 수 있으면 먼저 덤프를 뜬다. dev에도 개인정보가 있을 수 있으므로 소유자만 읽는
파일로 만들고, 서버 밖 승인된 로컬 환경으로 옮긴 뒤 서버 사본을 지운다.

```bash
umask 077
dump_dir="$(mktemp -d)"
sudo k3s kubectl -n dev-db exec postgres-0 -- sh -c 'pg_dump -U "$POSTGRES_USER" -Fc umc_product_dev' > "$dump_dir/umc_product_dev.dump"
sudo k3s kubectl -n dev-db exec -i postgres-0 -- pg_restore --list < "$dump_dir/umc_product_dev.dump" > "$dump_dir/umc_product_dev.toc"
sha256sum "$dump_dir/umc_product_dev.dump"
echo "$dump_dir"
```

`pg_restore --list`가 실패하면 덤프가 깨진 것이므로 진행하지 않는다. 로컬 터미널에서 출력된 경로의
dump를 `scp`로 받아 sha256이 같은지 확인한 뒤, 서버 사본을 지운다.

```bash
rm -- "$dump_dir/umc_product_dev.dump" "$dump_dir/umc_product_dev.toc"
rmdir -- "$dump_dir"
```

로컬 사본도 쓰임이 끝나면 지운다.

### 3. DB 지우고 다시 만들기

새 DB가 쓸 ICU `ko` collation과 libc `C.UTF-8` locale이 있는지 먼저 확인한다.

```bash
sudo k3s kubectl -n dev-db exec -i postgres-0 -- sh -c 'psql -X -At -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres' <<'SQL'
SELECT count(*) FROM pg_collation WHERE collprovider = 'i' AND collname LIKE 'ko%';
SQL
sudo k3s kubectl -n dev-db exec postgres-0 -- locale -a | grep -ix 'c\.utf-\?8'
```

첫 결과가 0보다 크고 두 번째 명령이 `C.utf8`을 출력하면 진행한다. 다음 명령부터 되돌릴 수 없다.
`WITH (FORCE)`가 앱 연결을 끊으며, 앱은 6단계까지 DB 에러를 낸다.

```bash
sudo k3s kubectl -n dev-db exec -i postgres-0 -- sh -c 'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres' <<'SQL'
DROP DATABASE umc_product_dev WITH (FORCE);
CREATE DATABASE umc_product_dev LOCALE_PROVIDER icu ICU_LOCALE 'ko-KR' LOCALE 'C.UTF-8' TEMPLATE template0 ENCODING UTF8;
SQL
```

DROP은 됐는데 CREATE가 실패했으면 DB가 없는 상태다. 앱을 재시작하기 전에 원인을 고치고 CREATE만
다시 실행한다.

```bash
sudo k3s kubectl -n dev-db exec -i postgres-0 -- sh -c 'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres' <<'SQL'
CREATE DATABASE umc_product_dev LOCALE_PROVIDER icu ICU_LOCALE 'ko-KR' LOCALE 'C.UTF-8' TEMPLATE template0 ENCODING UTF8;
SQL
```

### 4. 초기 설정 Job 다시 실행

새 DB에는 PostGIS·`btree_gist` 확장, 앱 role 권한, Preview 덤프 role 권한이 없다.
backup runbook의 [`rerun_role_jobs`](backup-activation.md#운영-db-복구와-role-job-재실행)를 셸에 정의한 뒤
두 Job을 다시 실행한다. 함수는 Job을 지우고 Argo CD `dev-postgres`가 다시 만들 때까지 기다린 뒤
`Complete`를 확인한다.

```bash
rerun_role_jobs dev-db postgres-app-role postgres-preview-dump-role
```

### 5. locale 확인

```bash
sudo k3s kubectl -n dev-db exec -i postgres-0 -- sh -c 'psql -X -At -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres' <<'SQL'
SELECT datlocprovider, datlocale, datcollate FROM pg_database WHERE datname = 'umc_product_dev';
SQL
```

`i|ko-KR|C.UTF-8`이어야 한다.

### 6. 앱 다시 띄우기

```bash
sudo k3s kubectl -n dev-app rollout restart deployment umc-product-server
sudo k3s kubectl -n dev-app rollout status deployment umc-product-server
```

앱이 뜨면서 Flyway가 기수·학교·지부 같은 골격 데이터를 다시 넣는다.

### 7. QA 시딩 확인

앱이 뜬 뒤 `QA 시딩 자동 실행` 로그를 5분까지 기다린다.

```bash
timeout 300 sh -c 'until sudo k3s kubectl -n dev-app logs deployment/umc-product-server --since=15m | grep "QA 시딩 자동 실행"; do sleep 10; done'
```

모든 단계가 `APPLIED`면 된다. 5분 안에 로그가 없으면 dev test API의 Basic Auth로
`POST https://api-dev.university.neordinary.com/test/seed/qa`를 한 번 부른다. QA 계정 비밀번호는
[dev QA 계정 비밀번호](../operations/secrets.md#dev-qa-계정-비밀번호)의 값이다.

### 8. 매칭 리허설 출발선

리허설을 바로 할 계획이면 서버 레포의
[프로젝트 매칭 리허설 문서](https://github.com/UMC-PRODUCT/umc-product-server/blob/develop/docs/onboarding/test/project-matching-rehearsal.md)를
따라 출발선을 만든다.

## 마무리

- 1단계에서 기록한 수동 권한과 설정이 있었다면 다시 준다.
- 2단계 덤프의 서버 사본을 지웠는지 확인한다.
- 팀 채널에 알리고 develop 머지를 다시 연다.

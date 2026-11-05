# dev DB 다시 만들기

dev `umc_product_dev`를 지우고 빈 DB로 다시 만든 뒤, Flyway와 QA 시딩으로 다시 채우는 절차다.
dev 데이터가 모두 사라지고 되돌릴 수 없다. prod·Preview DB에는 적용하지 않는다.

## 언제 쓰는가

- 테스트 데이터가 쌓여 dev를 처음 상태로 돌리고 싶을 때
- DB locale을 ICU `ko-KR`로 바꿔야 할 때. `POSTGRES_INITDB_ARGS`는 빈 데이터 디렉터리의
  최초 initdb에만 적용되므로, 이미 있는 DB는 이 절차로 다시 만들어야 바뀐다.

## 전제

- 실행자는 `umc-ssh-admins` 그룹으로 서버에 SSH 접속할 수 있다.

## 중단 조건

- 2단계 확인에서 ICU `ko` collation이 0개면 멈춘다. 이미지에 ICU가 없다는 뜻이다.
- 3단계 `rerun_role_jobs`가 실패하면 앱을 재시작하지 않는다. 확장과 앱 권한 없이
  Flyway가 돌면 첫 migration부터 실패한다.

## 절차

아래 명령은 서버에 SSH 접속한 뒤 실행한다.

### 1. (선택) 백업

지운 데이터가 필요할 수 있으면 먼저 덤프를 떠서 서버 밖에 보관한다. dev에도 개인정보가 있을 수
있으므로 승인된 로컬 환경에만 두고, 쓰임이 끝나면 지운다.

```bash
sudo k3s kubectl -n dev-db exec postgres-0 -- sh -c 'pg_dump -U "$POSTGRES_USER" -Fc umc_product_dev' > umc_product_dev.dump
```

### 2. DB 지우고 다시 만들기

```bash
sudo k3s kubectl -n dev-db exec postgres-0 -- sh -c 'psql -U "$POSTGRES_USER" -d postgres -Atc "SELECT count(*) FROM pg_collation WHERE collprovider = '"'"'i'"'"' AND collname LIKE '"'"'ko%'"'"'"'
```

결과가 0보다 크면 진행한다. 다음 명령부터 되돌릴 수 없다. `WITH (FORCE)`가 앱 연결을 끊으며,
앱은 5단계까지 DB 에러를 낸다.

```bash
sudo k3s kubectl -n dev-db exec postgres-0 -- sh -c 'psql -U "$POSTGRES_USER" -d postgres -c "DROP DATABASE umc_product_dev WITH (FORCE)" -c "CREATE DATABASE umc_product_dev LOCALE_PROVIDER icu ICU_LOCALE '"'"'ko-KR'"'"' LOCALE '"'"'C.UTF-8'"'"' TEMPLATE template0 ENCODING UTF8"'
```

### 3. 초기 설정 Job 다시 실행

새 DB에는 PostGIS·`btree_gist` 확장, 앱 role 권한, Preview 덤프 role 권한이 없다.
backup runbook의 [`rerun_role_jobs`](backup-activation.md#운영-db-복구와-role-job-재실행)를 셸에 정의한 뒤
두 Job을 다시 실행한다. 함수는 Job을 지우고 Argo CD `dev-postgres`가 다시 만들 때까지 기다린 뒤
`Complete`를 확인한다.

```bash
rerun_role_jobs dev-db postgres-app-role postgres-preview-dump-role
```

### 4. locale 확인

```bash
sudo k3s kubectl -n dev-db exec postgres-0 -- sh -c 'psql -U "$POSTGRES_USER" -d postgres -Atc "SELECT datlocprovider, datlocale, datcollate FROM pg_database WHERE datname = '"'"'umc_product_dev'"'"'"'
```

`i|ko-KR|C.UTF-8`이어야 한다.

### 5. 앱 다시 띄우기

```bash
sudo k3s kubectl -n dev-app rollout restart deployment umc-product-server
```

```bash
sudo k3s kubectl -n dev-app rollout status deployment umc-product-server
```

앱이 뜨면서 Flyway가 기수·학교·지부 같은 골격 데이터를 다시 넣는다.

### 6. QA 시딩 확인

앱이 뜬 뒤 `QA 시딩 자동 실행` 로그를 5분까지 기다린다.

```bash
timeout 300 sh -c 'until sudo k3s kubectl -n dev-app logs deployment/umc-product-server --since=15m | grep "QA 시딩 자동 실행"; do sleep 10; done'
```

모든 단계가 `APPLIED`면 된다. 5분 안에 로그가 없으면 dev test API의 Basic Auth로
`POST https://api-dev.university.neordinary.com/test/seed/qa`를 한 번 부른다.

### 7. 매칭 리허설 출발선

리허설을 바로 할 계획이면 서버 레포의
[프로젝트 매칭 리허설 문서](https://github.com/UMC-PRODUCT/umc-product-server/blob/develop/docs/onboarding/test/project-matching-rehearsal.md)를
따라 출발선을 만든다.

## 확인

- 4단계 locale이 `i|ko-KR|C.UTF-8`이다.
- 앱 readiness가 정상이고 Flyway 에러가 없다.
- QA 시딩 결과가 모두 `APPLIED`다.
- Preview 생성 시 dev 덤프가 성공한다. `postgres-preview-dump-role` Job이 새 DB에 권한을 다시 줬기 때문이다.

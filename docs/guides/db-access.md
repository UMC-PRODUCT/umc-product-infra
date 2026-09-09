# DB 접근

DB 운영은 플랫폼/인프라 관리자만 맡는다.
일반 maintainer에게 kubeconfig, ServiceAccount token, PostgreSQL 자격증명을 발급하지 않는다.
Grafana는 별도 public HTTPS + 사람별 Viewer 계정으로 제공하지만 DB 관리 권한과는 무관하다.
PostgreSQL 5432는 인터넷에 노출하지 않는다.

## 계정 경계

| 환경 | database | 앱 role | 모니터링 role | 관리자 Secret |
|---|---|---|---|---|
| prod | `umc_product` | `umc_product_app` | `umc_product_exporter` | `db/postgres-secrets` |
| dev | `umc_product_dev` | `umc_product_dev_app` | 없음 | `dev-db/postgres-secrets` |
| preview | `umc_product_pr<N>` | `umc_product_preview_app` 공유 | 없음 | `preview/postgres-preview-secrets` |

앱 role에는 superuser, createdb, createrole, replication 권한이 없다. 앱 password는 환경별 `app-db` Secret의 `DATABASE_PASSWORD`다.
앱은 관리자 Secret을 읽지 않는다. prod `umc_product_ro`는 조회 전용이다.
Git idempotent Job이 role과 grant를 만든다.

`umc_product_exporter`는 사람의 DB 접속용이 아니다. `pg_monitor`로 PostgreSQL 통계만
읽으며 앱 테이블 SELECT, superuser, createdb, createrole 권한은 없다. 별도
`postgres-exporter` Deployment가 이 role을 쓰므로 DB StatefulSet은 모니터링 변경 때
재시작되지 않는다.

## 접속 원칙

관리자 PC와 IDC 노드가 모두 tailnet에 연결된 상태에서, Tailscale 경유 기존
OpenSSH+public key/PEM으로 접속한다. Tailscale SSH는 사용하지 않는다.
root-only kubeconfig는 복사하지 않고 IDC SSH 후 `sudo -n k3s kubectl`을 쓴다.
Kubernetes API `6443/tcp`는 공인 또는 tailnet 접속용으로 열지 않는다.
password는 승인된 비밀 관리 도구에서 가져와 psql prompt 또는 DataGrip의 Password 필드에 입력한다.
Secret 값을 `k3s kubectl get -o yaml`, 셸 인자, history, 채팅에 출력하지 않는다.

## DataGrip에서 SSH로 접속

이미 IDC 관리 권한과 개인 SSH 키가 있는 관리자용 절차다. 일반 팀원에게 root 계정이나
관리자의 개인 키를 공유하는 방법이 아니다. PC와 서버 모두 Tailscale에 연결되어 있어야 한다.

DataGrip의 내장 SSH tunnel은 IDC 노드에서 PostgreSQL Service의 내부 IP로 연결한다.
노드에서 해당 Service에 접근할 수 있으면 별도 `ssh -L`이나 `kubectl port-forward` 터미널을
유지할 필요가 없다. 인터넷이나 tailnet에 PostgreSQL `5432/tcp`를 새로 열지 않는다.

### 1. DB 내부 주소 확인

승인된 Tailscale OpenSSH 세션으로 IDC에 접속한 뒤 아래 명령을 실행한다. Secret은 조회하지 않는다.

```bash
# prod DB Host
sudo -n k3s kubectl -n db get service postgres -o jsonpath='{.spec.clusterIP}{"\n"}'

# dev DB Host
sudo -n k3s kubectl -n dev-db get service postgres -o jsonpath='{.spec.clusterIP}{"\n"}'
```

출력된 주소는 **DB Host**이며 서버의 공인 IP 또는 Tailscale IP와 다르다. Service의 ClusterIP는
Pod 재시작에는 유지되지만 Service 재생성이나 새 클러스터 설치 후에는 달라질 수 있으므로
고정 주소로 문서에 복사하지 않는다. 클러스터 Service DNS도 노드의 OS에서는 해석되지 않을 수 있다.

### 2. DataGrip SSH 구성

PostgreSQL Data Source를 만들고 `SSH/SSL` 탭에서 `Use SSH tunnel`을 켠다.
SSH 구성을 다음과 같이 등록하며 dev/prod 연결에 같은 구성을 사용한다.

| SSH 항목 | 값 |
|---|---|
| Host | 실제 inventory의 Tailscale IP 또는 MagicDNS 이름 |
| Port | `22` |
| User name | 승인된 IDC SSH 사용자 |
| Authentication type | `Key pair (OpenSSH or PuTTY)` |
| Private key file | 본인 개인 키 경로 (`.pub` 파일이 아님) |
| Passphrase | 개인 키에 설정된 경우에만 입력 |
| Local port | 자동 할당 유지 |

엄격한 호스트 키 검사를 활성화하고 최초 연결 지문은 신뢰된 console 또는 기존 검증된 SSH
세션에서 얻은 서버 공개키 지문과 비교한다. 키 불일치 경고를 무시하지 않는다.
SSH `Test Connection` 성공은 서버 로그인 확인이며, DB 로그인 성공과는 별개다.

### 3. 환경별 General 설정

| 항목 | dev | prod |
|---|---|---|
| Name | `UMC DEV` | `UMC PROD 읽기전용` |
| Host | 1단계의 `dev-db/postgres` ClusterIP | 1단계의 `db/postgres` ClusterIP |
| Port | `5432` | `5432` |
| Authentication | `User & Password` | `User & Password` |
| User | `umc_product_dev_app` | `umc_product_ro` |
| Database | `umc_product_dev` | `umc_product` |
| Password 원본 | `/umc-product/dev/app-db`의 `DATABASE_PASSWORD` | `/umc-product/prod/postgres-readonly`의 `RO_PASSWORD` |

운영자가 로컬 원장을 최신으로 유지했다면 각각 `.env.dev`의 `DATABASE_PASSWORD`,
`.env.prod`의 `RO_PASSWORD`를 사용할 수 있다. 파일 전체를 공유하거나 비밀번호를 JDBC URL에 넣지 않는다.
현재 PostgreSQL 구성은 별도 DB TLS를 사용하지 않으므로 `Use SSL`은 끈다.
원격 접속 구간은 SSH와 Tailscale로 암호화한다.

드라이버가 없으면 `Download missing driver files`를 누른 뒤 DB `Test Connection`을 실행한다.
연결 후 SQL console에서 다음 조회로 환경을 확인한다. 테이블이 보이지 않으면 `Schemas`에서 `public`을 선택한다.

```sql
SELECT current_database(), current_user;
```

prod는 DB 권한 자체가 조회 전용이다. dev 앱 계정은 데이터 변경이 가능하므로 작업 환경을 먼저 확인한다.
SSH는 성공하지만 DB 연결이 거부되면 Service IP와 Pod 상태, 노드에서 해당 IP의 `5432/tcp` 접근,
SSH TCP forwarding 허용 여부를 확인한다. 공인 DB port나 추가 접근 권한을 열어 해결하지 않는다.
아래 터미널 방식과 달리 DataGrip 내장 tunnel에서는 General Host에 `127.0.0.1`을 넣지 않는다.

UI 항목은 [DataGrip PostgreSQL 연결](https://www.jetbrains.com/help/datagrip/postgresql.html)과
[내장 SSH tunnel 안내](https://www.jetbrains.com/help/datagrip/configuring-ssh-and-ssl.html#connect-to-a-database-with-ssh)를 참고한다.

## 터미널에서 SSH와 port-forward로 접속

로컬 psql이 필요하면 노드 루프백에만 Kubernetes port-forward를 열고, 관리자 PC의
루프백까지 SSH tunnel로 이어 준다. 아래 `IDC_NODE_HOST`는 Tailscale MagicDNS 이름 또는
Tailscale IP여야 한다.

prod readonly 접속(첫 번째 터미널):

```bash
IDC_SSH_USER="${IDC_SSH_USER:?inventory의 SSH 사용자를 설정하세요}"
IDC_NODE_HOST="${IDC_NODE_HOST:?Tailscale MagicDNS 이름 또는 IP를 설정하세요}"
ssh -L 127.0.0.1:15432:127.0.0.1:15432 \
  "${IDC_SSH_USER}@${IDC_NODE_HOST}" \
  'sudo -n k3s kubectl port-forward --address=127.0.0.1 -n db pod/postgres-0 15432:5432'
```

prod readonly 접속(두 번째 터미널):

```bash
psql 'host=127.0.0.1 port=15432 dbname=umc_product user=umc_product_ro sslmode=disable'
```

dev 점검(첫 번째 터미널):

```bash
IDC_SSH_USER="${IDC_SSH_USER:?inventory의 SSH 사용자를 설정하세요}"
IDC_NODE_HOST="${IDC_NODE_HOST:?Tailscale MagicDNS 이름 또는 IP를 설정하세요}"
ssh -L 127.0.0.1:15433:127.0.0.1:15433 \
  "${IDC_SSH_USER}@${IDC_NODE_HOST}" \
  'sudo -n k3s kubectl port-forward --address=127.0.0.1 -n dev-db pod/postgres-0 15433:5432'
```

dev 점검(두 번째 터미널):

```bash
psql 'host=127.0.0.1 port=15433 dbname=umc_product_dev user=umc_product_dev_app sslmode=disable'
```

port-forward는 `127.0.0.1`에만 bind한다. `--address 0.0.0.0`은 금지한다.
Pod-to-Pod NetworkPolicy 검증용이 아니다.

## 관리자 작업

관리자 role은 restore, role bootstrap 장애, 승인된 migration에만 쓴다. 평상시 조회는 `umc_product_ro`로 한다.

관리자 psql(노드 내부, 값 미노출):

```bash
sudo -n k3s kubectl exec -it -n db postgres-0 -- \
  sh -ceu 'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

수동 생성 role과 grant는 Argo sync에서 재현되지 않는다. 지속 변경은 `app-role-job.yaml` 또는 `readonly-role-job.yaml`에 반영한다.
앱 schema owner는 prod `umc_product_app`, dev `umc_product_dev_app`이다.

## readonly password 회전

원본: `/umc-product/prod/postgres-readonly`의 `RO_PASSWORD`. AWS Console에서 JSON 전체를 확인하고 property만 바꾼다.
Secret은 CLI 인자에 넣지 않는다.
아래 클러스터 명령은 `IDC_NODE_HOST`에 tailnet OpenSSH로 접속한 세션에서 실행한다.

```bash
before_refresh="$(sudo -n k3s kubectl get externalsecret postgres-readonly -n db \
  -o jsonpath='{.status.refreshTime}')"
sudo -n k3s kubectl annotate externalsecret postgres-readonly -n db \
  external-secrets.io/force-sync="$(date +%s)" --overwrite

attempts=0
while :; do
  after_refresh="$(sudo -n k3s kubectl get externalsecret postgres-readonly -n db \
    -o jsonpath='{.status.refreshTime}')"
  ready="$(sudo -n k3s kubectl get externalsecret postgres-readonly -n db \
    -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}')"
  if test "$ready" = True && test -n "$after_refresh" \
    && test "$after_refresh" != "$before_refresh"; then
    break
  fi
  attempts=$((attempts + 1))
  test "$attempts" -lt 90 || exit 1
  sleep 2
done

sudo -n k3s kubectl delete job/postgres-readonly-role -n db
sudo -n k3s kubectl wait --for=create job/postgres-readonly-role -n db --timeout=300s
sudo -n k3s kubectl wait --for=condition=Complete job/postgres-readonly-role -n db --timeout=300s
```

ExternalSecret 갱신만으로 PostgreSQL role password는 바뀌지 않는다.
마지막 Job이 `ALTER ROLE`을 완료한 뒤 새 password로 readonly 접속과 쓰기 거부를 확인한다.
두 검증이 성공하면 이전 값을 폐기한다.

```sql
SELECT current_user, current_database();
SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public';
CREATE TABLE must_fail(id integer);
```

마지막 SQL은 권한 오류로 실패해야 한다.

## 앱 password 회전

Secret과 실제 role을 바꾸고 Pod를 재시작한다.
환경별 순서는 [시크릿 운영 문서](secrets.md#앱-db-비밀번호-회전)를 따른다.
관리자와 앱 password를 같은 값으로 쓰지 않는다.

## 점검

```bash
sudo -n k3s kubectl get pod -n db -l app.kubernetes.io/name=postgres
sudo -n k3s kubectl get job -n db postgres-app-role postgres-readonly-role postgres-exporter-role
sudo -n k3s kubectl get deployment,service -n db -l app.kubernetes.io/name=postgres-exporter
sudo -n k3s kubectl get networkpolicy -n db
sudo -n k3s kubectl logs -n db job/postgres-app-role
sudo -n k3s kubectl logs -n db job/postgres-readonly-role
sudo -n k3s kubectl logs -n db deployment/postgres-exporter
```

`Connection refused`가 Job 직후만 나면 NetworkPolicy endpoint 반영 지연일 수 있다.
bootstrap Job의 재시도와 deadline은 제한된다.
반복 실패 시 Secret key 이름, Service DNS, NetworkPolicy selector, PostgreSQL readiness를 확인하며 password 값은 출력하지 않는다.

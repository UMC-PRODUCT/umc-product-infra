# DB 접근

DB 운영은 플랫폼/인프라 관리자만 맡는다.
일반 maintainer에게 kubeconfig, ServiceAccount token, PostgreSQL 자격증명을 발급하지 않는다.
Grafana는 별도 public HTTPS + 사람별 Viewer 계정으로 제공하지만 DB 관리 권한과는 무관하다.
PostgreSQL 5432는 인터넷에 노출하지 않는다.

## 계정 경계

| 환경 | database | 앱 role | 관리자 Secret |
|---|---|---|---|
| prod | `umc_product` | `umc_product_app` | `db/postgres-secrets` |
| dev | `umc_product_dev` | `umc_product_dev_app` | `dev-db/postgres-secrets` |
| preview | `umc_product_pr<N>` | `umc_product_preview_app` 공유 | `preview/postgres-preview-secrets` |

앱 role에는 superuser, createdb, createrole, replication 권한이 없다. 앱 password는 환경별 `app-db` Secret의 `DATABASE_PASSWORD`다.
앱은 관리자 Secret을 읽지 않는다. prod `umc_product_ro`는 조회 전용이다.
Git idempotent Job이 role과 grant를 만든다.

## 접속 원칙

관리자 PC와 IDC 노드가 모두 tailnet에 연결된 상태에서, Tailscale 경유 기존
OpenSSH+public key/PEM으로 접속한다. Tailscale SSH는 사용하지 않는다.
root-only kubeconfig는 복사하지 않고 IDC SSH 후 `sudo -n k3s kubectl`을 쓴다.
Kubernetes API `6443/tcp`는 공인 또는 tailnet 접속용으로 열지 않는다.
password는 승인된 비밀 관리 도구에서 가져와 psql prompt로 입력한다.
Secret 값을 `k3s kubectl get -o yaml`, 셸 인자, history, 채팅에 출력하지 않는다.

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
sudo -n k3s kubectl get job -n db postgres-app-role postgres-readonly-role
sudo -n k3s kubectl get networkpolicy -n db
sudo -n k3s kubectl logs -n db job/postgres-app-role
sudo -n k3s kubectl logs -n db job/postgres-readonly-role
```

`Connection refused`가 Job 직후만 나면 NetworkPolicy endpoint 반영 지연일 수 있다.
bootstrap Job의 재시도와 deadline은 제한된다.
반복 실패 시 Secret key 이름, Service DNS, NetworkPolicy selector, PostgreSQL readiness를 확인하며 password 값은 출력하지 않는다.

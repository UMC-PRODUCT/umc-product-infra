# Preview 활성화 준비와 장애 대응

Preview를 준비하거나 배포·DB 정리 실패를 조사하는 인프라 담당자용 문서다.
팀원에게는 [Preview 사용 가이드](../guides/preview-environments.md)를 공유한다.
클러스터 명령은 개인 관리자 SSH로 접속한 IDC에서 실행하며 kubeconfig를 PC로 복사하지 않는다.
아래 PR 27은 예시이므로 실제 조사 대상 번호로 바꾼다.

## 활성화 전 확인

다음은 활성화에 필요한 조건이며, 이미 적용되었다는 뜻이 아니다.
[ApplicationSet](../../argocd/applications/preview/applicationset.yaml),
[Preview values](../../charts/umc-product-server/values-preview.yaml)와 실제 배포 결과를 함께 확인한다.

### 이미지 발행과 배포 연결

1. 같은 저장소에서 `develop`으로 보내는 PR인지, 작성자가 `MEMBER`·`OWNER`·`COLLABORATOR`인지 확인한다.
   `preview` 라벨 권한은 신뢰된 관리자에게만 부여하고 fork·외부 코드를 실행하지 않는다.
2. 백엔드 trusted CI가 PR 이미지를 발행하고 익명 pull을 검증해야 한다. 같은 SHA 태그를 덮어쓰지 않는다.
   빌드와 배포 자격증명을 쓰는 작업을 분리한다. 상세 조건은 [배포 자동화 계약](../operations/deployment.md#backend-배포-자동화-계약)을 따른다.
3. 성공한 SHA·tag·digest를 `argocd/preview-releases/pr-<번호>.json`에 기록하고,
   ApplicationSet이 열린 PR·라벨과 이 기록을 함께 선택하도록 연결한다.
   **PR head만 선택하는 설정으로는 성공 이미지 연동 조건을 충족하지 않는다.**
4. 첫 성공 기록이 없으면 앱·DB를 만들지 않고, 후속 빌드가 실패하면 이전 성공 기록을 유지하는지 검증한다.
   PR 종료 후 기록이 남아도 앱이 다시 생성되지 않아야 한다. 열린 PR의 기록을 일시 정지 목적으로 삭제하지 않는다.
5. PR별 이미지·DB·URL 및 `DEMODAY_QR_BASE_URL`을 주입한 뒤 앱과 Ingress를 활성화한다.
   PR별 값 없이 공통 `values-preview.yaml`만 활성화하지 않는다.

일반 PR CI 성공, 이미지 발행 성공, infra 갱신과 앱 기동은 별개다.
성공 이미지 연결도 기동·Flyway 실패나 DB 변경을 자동으로 복구하지 않는다.
배포 전략은 [Deployment 템플릿](../../charts/umc-product-server/templates/deployment.yaml)에서 확인한다.
Git에 복구할 이미지를 반영하는 작업과 DB 복원은 따로 판단한다.

### 자격증명과 외부 기능

- PR 조회 PAT: `/umc-product/platform/argocd/preview-github-token`. 조회에 필요한 읽기 권한만 부여한다.
- 앱 자격증명: `/umc-product/preview/app-{db,jwt,oauth,storage,email}`.
  `app-email`의 nonprod SES 자격증명 공유를 제외하고 prod/dev와 분리하며 prod sender는 공유하지 않는다.
- DB 앱 계정: `app-db`의 `umc_product_preview_app`. 관리자 계정은
  `/umc-product/preview/postgres-preview-secrets`로 분리하며 platform `secrets` Application이 관리한다.
- 앱·DB Secret 및 Argo CD 토큰 ExternalSecret이 `Ready=True`인지 확인한다. 비밀값은 출력하지 않는다.
- GHCR은 public·익명 pull을 사용한다. `ghcr-pull` Secret을 별도로 주입하지 않는다.
- Preview는 SES를 사용한다. 수신자 검증·발송 가능 여부는 [이메일 운영 절차](../operations/secrets.md)를 따른다.
- QR이 여는 웹 주소는 API 주소와 별개다. 기존 웹이 자동으로 PR API를 사용하지 않는다.
  모바일 provider token 로그인은 검증하되 웹 OAuth 동적 콜백이나 callback broker가 있다고 가정하지 않는다.

### TLS와 수용량

- 공용 `preview/preview-wildcard` Certificate는 `*.university.neordinary.com`에 대해
  `letsencrypt-production`을 사용한다. 최신 generation의 `Ready=True`와 `preview-wildcard-tls` 존재를 확인한다.
- Route53 A/TXT는 Ingress별로 ExternalDNS가 관리한다. wildcard DNS 레코드를 쓰는 구조가 아니며,
  API 요청은 IDC의 Traefik `443/TCP`로 직접 들어온다.
- API 자체 인증과 문서용 접근 보호는 별개다. 문서 보호 Ingress·인증 Secret·외부 요청 결과를 확인한다.
  문서 보호 설정이 꺼져 있다고 문서 경로까지 차단된 것으로 판단하지 않는다.
- 동시 Preview 상한은 3개다. [ResourceQuota](../../manifests/cluster/preview-resourcequota.yaml)는
  PostgreSQL Service 2개와 PR별 앱 Service 3개, namespace의 CPU·메모리를 제한한다.
  앱 Service가 wave `-2`, createdb Job이 `-1` 순으로 처리되어야 한다.
- 3개가 실행 중일 때 네 번째 Service가 quota에서 거부되고 DB가 먼저 생성되지 않는지 검증한다.
  fork PR은 라벨이 붙더라도 trusted CI가 이미지 발행을 거부하는지 확인한다.

## 공유 범위와 보안 경계

모든 PR은 `preview` namespace와 PostgreSQL Pod 한 대를 공유하고 database만 PR별로 나눈다.
앱의 DB·JWT·OAuth·스토리지·이메일 자격증명과 Demoday 키도 공유하므로 PR 간 강한 격리는 없다.
`umc_product_preview_app`는 여러 PR DB의 owner다. 운영 데이터나 실제 비밀값을 테스트 데이터로 넣지 않는다.

PSA `restricted:v1.36`과 default-deny NetworkPolicy를 유지한다. 앱은 DNS, Preview DB,
OTel Collector, private CIDR을 제외한 public HTTPS를 사용하고 prod/dev DB로는 접근하지 않는다.
이 정책만으로 다른 Preview DB 접근이나 HTTPS를 통한 Preview 자격증명 유출까지 막지는 못한다.
상호 불신 코드를 실행해야 한다면 PR별 namespace·credential·DB role·NetworkPolicy 분리를 먼저 설계한다.

## 배포 또는 접속 실패

먼저 해당 PR의 이미지 발행 결과와 실제 배포 SHA를 대조한다. 이후 리소스 상태를 확인한다.

```bash
sudo k3s kubectl get application -n argocd umc-product-preview-27
sudo k3s kubectl get certificate -n preview preview-wildcard
sudo k3s kubectl get deployment/umc-product-server-pr-27 service/umc-product-server-pr-27 \
  job/umc-product-server-pr-27-createdb -n preview
```

내부 readiness 확인이 필요하면 IDC SSH 세션에서 다음 터널을 유지한다.

```bash
sudo k3s kubectl port-forward -n preview deployment/umc-product-server-pr-27 19090:9090
```

IDC의 다른 SSH 세션에서 실행한다.

```bash
curl -fsS http://127.0.0.1:19090/actuator/health/readiness
```

내부 readiness 성공과 외부 접속 성공은 별개다. 외부에서 해당 Preview의 DNS·TLS·API 인증·응답을
확인한다. Actuator `9090`을 외부에 노출하지 않는다. Grafana 사용은
[모니터링 접근](../operations/tool-access.md)을 따른다.

## DB 생성 실패

[createdb Job](../../charts/umc-product-server/templates/createdb-job.yaml)은 앱보다 먼저 실행한다.
없는 `umc_product_pr<N>` DB만 생성하며 owner는 `umc_product_preview_app`로 제한한다.
관리자로 `postgis`·`btree_gist`를 준비한다. 관리자 Secret은 PR Application이 아니라 platform에서 유지한다.

```bash
sudo k3s kubectl get job -n preview umc-product-server-pr-27-createdb
sudo k3s kubectl logs -n preview job/umc-product-server-pr-27-createdb
```

Job의 원인을 수정하고 동기화를 재시도한다. 기존 DB를 초기화해서 우회하지 않는다.
팀원의 DB 접속은 개인 SSH `db_tunnel`과 별도 PostgreSQL 계정을 발급하고, 현재
`preview/postgres-preview` Service ClusterIP만 `permit_open`에 허용한다.
[SSH·DB 접근 관리](../operations/ssh-access.md#개인-계정과-db-터널)를 따른다.

## DB 삭제 실패

PR close 또는 라벨 제거 시 workload가 정리된 뒤
[PostDelete hook](../../charts/umc-product-server/templates/dropdb-job.yaml)이 같은 `createDatabase.name`을 삭제한다.
DNS는 ExternalDNS가 소유한 PR별 레코드만 정리하며, 공용 Certificate와 TLS Secret은 유지한다.

삭제 대상은 렌더링·실행 시 모두 `^umc_product_pr[0-9]+$`로 제한한다.
`postgres`·template·bootstrap·prod·dev DB는 거부하며 접속 시각이나 파일 시각으로 대상을 추측하지 않는다.
`DROP DATABASE ... WITH (FORCE)`는 **해당 DB의 연결과 데이터를 삭제**한다. 다른 DB 연결은 종료하지 않는다.

권한·Secret·DB 장애, prepared transaction, 활성 logical replication slot 또는 subscription 등으로
삭제가 실패하면 `DeletionError`와 실패 Job을 확인한다.

```bash
sudo k3s kubectl get application -n argocd umc-product-preview-27
sudo k3s kubectl get job -n preview umc-product-server-pr-27-dropdb
sudo k3s kubectl logs -n preview job/umc-product-server-pr-27-dropdb
```

원인을 고친 뒤 Application 삭제를 재시도한다. 실패 로그를 확인하기 전에 Job을 지우거나
finalizer를 제거해 삭제를 우회하지 않는다.

### 최후 수단: 수동 삭제

hook을 복구할 수 없을 때만 인프라 관리자가 수행한다. PR 종료·라벨 제거와 해당 앱 종료를
확인하고, Application의 DB 이름과 삭제 대상을 대조한다. 대상이 불명확하면 중단한다.
필요한 데이터의 보관이 끝났는지 확인한다. 실행하면 해당 DB의 DataGrip 연결도 끊기며 백업 없이는 복구할 수 없다.

아래 `CHANGE_ME`를 확인한 PR 번호로 바꾼다. 그대로 실행하면 이름 검사에서 거부된다.
Preview PostgreSQL의 bootstrap DB에 접속하며 비밀값은 출력하지 않는다.

```bash
sudo k3s kubectl exec -it -n preview postgres-preview-0 -- env PREVIEW_PR=CHANGE_ME sh -ceu '
  case "$PREVIEW_PR" in ""|*[!0-9]*) exit 64 ;; esac
  target_db="umc_product_pr${PREVIEW_PR}"
  exec dropdb --force --maintenance-db="$POSTGRES_DB" -U "$POSTGRES_USER" "$target_db"
'
```

삭제 후 해당 DB가 없어졌는지와 Application 정리 완료를 확인한다. 다른 PR·dev·prod 리소스는 건드리지 않는다.

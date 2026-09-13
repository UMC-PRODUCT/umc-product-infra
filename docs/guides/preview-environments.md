# PR 프리뷰

대상은 내부의 신뢰된 same-repository PR뿐이다. fork와 외부 PR에는 `preview` label을 붙이거나 Preview를 배포하지 않는다.
동시 상한은 3개다. URL은 `https://api-pr-<PR번호>.university.neordinary.com`이며 모바일 앱이 직접 호출한다.

> [!IMPORTANT]
> 이 문서는 **성공 이미지 연동을 적용한 Preview 운영 구조**를 설명한다. 이미지 발행, infra의 성공 이미지 선택,
> 앱·Ingress gate와 공용 TLS 준비를 각각 확인해야 한다. PR 번호를 URL에 넣거나
> `preview` label만 붙였다고 접속 준비가 끝난 것은 아니다.
> 활성화 준비는 [사용 조건](#사용-조건)과 [운영 준비](#운영-준비)를 따른다.

## 사용 조건

PR은 다음 조건을 모두 만족해야 한다.

- `umc-product-server` 저장소에서 만든 branch다.
- target branch는 `develop`이다.
- 작성자는 `MEMBER`, `OWNER`, `COLLABORATOR` 중 하나다.
- maintainer가 head repository와 작성자 관계를 확인했다.
- maintainer가 `preview` label을 붙였다.
- backend trusted CI가 해당 PR head SHA image를 GHCR에 발행하고 익명 pull을 검증했다.
- infra ApplicationSet이 PR별 성공 기록에서 image tag·digest를 선택한다.
- ApplicationSet이 PR별 값과 함께 `deployment.enabled`와 `ingress.enabled`를 `true`로 주입한다.
  공통 `values-preview.yaml`만 직접 활성화하지 않는다.

백엔드의 `.github/workflows/publish-preview.yml`은 trusted PR을 별도 job에서 빌드·발행하고,
성공한 SHA·tag·digest를 infra의 `argocd/preview-releases/pr-<번호>.json`에 기록한다.
일반 PR CI 성공과 이 workflow의 발행·infra 갱신 성공은 별개다. 실제 Actions run과 infra commit을 확인한다.

infra의 [ApplicationSet](../../argocd/applications/preview/applicationset.yaml)은 열린 PR·라벨과 성공 release 정보를
함께 선택한다. 최초 성공 기록이 없으면 앱과 DB를 만들지 않고, 후속 빌드·발행 실패로 기록이
바뀌지 않으면 마지막 성공 이미지를 유지한다. 최신 PR head와 배포된 SHA는 다를 수 있다.
ApplicationSet PR generator는 author association을 직접 필터링하지 못하고 `preview` label을 본다.
따라서 `preview` label은 신뢰 검토를 끝냈다는 보안 승인이다. fork나 외부 PR에 붙이지 않으며,
외부 작성자는 label을 직접 붙일 권한을 갖지 않는다.
trusted publish CI는 같은 12자리 head-SHA tag를 덮어쓰지 않는다. Preview는 registry digest를
포함한 성공 release 정보를 Git에 기록하고, 배포 쪽에서도 이를 소비해야 한다.

## 수명주기

```text
Preview 활성화 전 공용 TLS 준비
  -> Certificate: preview/preview-wildcard (*.university.neordinary.com)
  -> Secret: preview/preview-wildcard-tls

trusted PR + preview label
  -> backend image:<head SHA 12자리> 발행·익명 pull 검증
  -> 성공 release 정보 Git 반영
  -> ApplicationSet poll + 성공 release 선택
  -> Argo Application
  -> Service admission (quota, wave -2)
  -> createdb Job (wave -1)
  -> Deployment + Ingress (공용 TLS Secret 사용)
  -> ExternalDNS exact record

label 제거 또는 PR close
  -> workload prune
  -> ExternalDNS exact record 정리
  -> PostDelete dropdb Job
  -> Application 삭제
```

ApplicationSet은 PR과 Git 성공 기록을 각각 120초마다 확인한다. 이름은 Application `umc-product-preview-<PR번호>`,
workload `umc-product-server-pr-<PR번호>`, database `umc_product_pr<PR번호>`,
관측 service `preview-umc-product-pr<PR번호>`다.

성공 기록은 PR 종료 후에도 남을 수 있다. 열린 PR과 라벨이 없으면 기록만으로 앱을 만들지 않는다.
반대로 열린 PR의 성공 기록을 삭제하면 앱·DB 삭제로 이어질 수 있으므로 일시 정지 용도로 지우지 않는다.
PR을 다시 열더라도 이미 삭제한 DB 데이터는 복구되지 않는다.

## 격리 범위

모든 프리뷰는 `preview` namespace와 PostgreSQL Pod 한 대를 공유하고 PR별 database를 쓴다.
ResourceQuota의 `count/services: 5`는 고정 PostgreSQL Service 2개와 PR별 앱 Service 최대 3개를 허용하고,
namespace의 CPU·memory 예산도 제한한다. 앱 Service는 sync wave `-2`에서 createdb Job의 `-1`보다
먼저 자리를 예약한다. 세 자리가 차면 네 번째 Service admission이 실패하므로 database만 먼저
만들어지지 않는다.
PSA `restricted:v1.36`이 enforce되고 default-deny NetworkPolicy가 ingress/egress를 차단한다.
앱은 DNS, preview PostgreSQL, OTel Collector, private CIDR을 제외한 public HTTPS만 사용할 수 있다.
prod와 dev DB 경로는 허용하지 않는다.

PR 간 강한 격리는 없다. 모든 PR이 `app-db`, `app-jwt`, `app-oauth`, `app-storage`, `app-email`
Secret과 `umc_product_preview_app` role을 공유하고, role은 여러 `umc_product_pr<N>` database를 소유한다.
`app-jwt` 안의 JWT와 Demoday 암호화·서명 키도 모든 preview PR이 공유한다.
신뢰된 내부 코드도 다른 preview DB에 접근하거나 public HTTPS로 preview Secret을 내보낼 수 있다.
따라서 상호 불신 코드, fork, 외부 기여 코드는 절대 실행하지 않는다. 실행이 필요해지면 PR별
credential, namespace, NetworkPolicy, database role을 함께 분리한다.

## 확인 방법

예를 들어 활성화한 PR 27은 Route53에 `api-pr-27.university.neordinary.com` exact A record와 ExternalDNS TXT ownership
record를 가진다. Route53은 DNS만 제공하며 모바일 앱은 조회된 IDC public IPv4의 Traefik `443/TCP`로
직접 HTTPS 요청한다.
TLS는 `preview` namespace의 `*.university.neordinary.com` 공용 Certificate가 만든 `preview-wildcard-tls` Secret을 사용한다.
모바일 직접 호출 API에는 별도 앞단 로그인 정책을 적용하지 않는다.

Grafana public gate를 통과한 뒤 일반 maintainer는 VPN 없이
`https://grafana.university.neordinary.com`에 개인 `Viewer` 계정으로 로그인해
`preview-umc-product-pr<번호>`의 log, metric, trace를 본다. gate 전 local-only health 점검과
kubectl은 인프라 관리자만 사용한다. 자세한 경계는
[모니터링 접근 가이드](monitoring-access.md)를 따른다.

클러스터 명령은 개인 관리자 SSH로 접속한 IDC에서 실행한다. port-forward는 해당 SSH 세션에서
유지하고, readiness 확인은 IDC의 다른 SSH 세션에서 실행한다. kubeconfig를 PC에 복사하지 않는다.

```bash
sudo k3s kubectl get application -n argocd umc-product-preview-27
sudo k3s kubectl get certificate -n preview preview-wildcard
sudo k3s kubectl get deployment/umc-product-server-pr-27 service/umc-product-server-pr-27 \
  job/umc-product-server-pr-27-createdb -n preview
sudo k3s kubectl port-forward -n preview deployment/umc-product-server-pr-27 \
  18080:8080 19090:9090
# IDC의 다른 SSH 세션에서 실행
curl -fsS http://127.0.0.1:19090/actuator/health/readiness
```

외부에서 모바일 앱이나 public API endpoint로 `api-pr-27.university.neordinary.com`의 TLS, API 인증,
응답을 확인한다. cluster 내부용 Actuator `9090`은 외부 Ingress 확인에 쓰지 않는다.

ExternalDNS는 Ingress별 exact record만 만들고 wildcard record를 쓰지 않는다. PR close나 label 제거로
Ingress가 사라지면 ExternalDNS가 자신이 소유한 record를 정리한다. 공용 wildcard Certificate와
`preview-wildcard-tls`는 PR 수명주기와 분리되어 유지된다.

모바일 네이티브 Google/Kakao/Apple 로그인은 앱이 provider token을 받아 preview API의 login
endpoint로 전달하는 흐름을 검증한다.
브라우저/web OAuth 동적 redirect callback과 preview용 고정 callback broker는 지원하지 않는다.
preview OAuth credential은 prod/dev와 분리해 `/umc-product/preview/app-oauth`에서 공급한다.

## database 생성

createdb Job은 앱보다 먼저 실행하며 owner는 `umc_product_preview_app`만 허용한다.
없는 `umc_product_pr<N>` database만 만들어 재실행해도 안전하며, 관리자로 `postgis`와 `btree_gist`를 선설치한다.
admin Secret은 platform `secrets` Application이 상시 관리한다.

```bash
sudo k3s kubectl get job -n preview umc-product-server-pr-27-createdb
sudo k3s kubectl logs -n preview job/umc-product-server-pr-27-createdb
```

## 개인 DB 접속

사람의 DB 접속은 공인 SSH의 개인 `db_tunnel` 계정과 별도 개인 PostgreSQL 계정을 사용한다.
운영자가 현재 `preview/postgres-preview` Service ClusterIP를 조회해 그 사람의 `permit_open`을
지정하고, DataGrip Database에는 생성된 `umc_product_pr<번호>`를 넣는다. DB 포트를 공개하거나
앱 공용 role을 팀원에게 배포하지 않는다. 상세 입력은
[개인 계정과 DB 터널](ansible-bootstrap.md#개인-계정과-db-터널)을 따른다.

## database 삭제

PR close/label 제거 시 Argo CD가 Application 리소스를 prune한 뒤 `PostDelete` hook이
같은 `createDatabase.name`을 삭제한다. render/runtime은 `^umc_product_pr[0-9]+$`만 허용하고
`postgres`, template, bootstrap, prod, dev를 거부한다. cleanup은 접속/파일 시각을 추측하지 않는다.

dropdb Job은 `DROP DATABASE ... WITH (FORCE)`로 해당 PR DB의 기존 연결을 강제 종료하고
DB 전체를 삭제한다. DataGrip의 작업 중 연결도 끊기며, 삭제된 데이터는 백업 없이는 복구할 수 없다.
다른 PR이나 dev/prod DB 연결은 종료하지 않는다. 권한·Secret·DB 장애, prepared transaction,
활성 logical replication slot 또는 subscription 등으로 DROP이 실패하면 Application은
`DeletionError`로 남고, 실패 Job도 원인 확인용으로 남는다. 원인을 고친 뒤 Application 삭제를 재시도한다.

```bash
sudo k3s kubectl get application -n argocd umc-product-preview-27
sudo k3s kubectl get job -n preview umc-product-server-pr-27-dropdb
sudo k3s kubectl logs -n preview job/umc-product-server-pr-27-dropdb
```

수동 DROP은 hook 복구 불가 시만 인프라 관리자가 대상 이름을 재확인하고 preview bootstrap
database에 접속해 수행한다.

```bash
sudo k3s kubectl exec -it -n preview postgres-preview-0 -- sh -ceu '
  target_db=umc_product_pr27
  suffix="${target_db#umc_product_pr}"
  case "$suffix" in ""|*[!0-9]*) exit 64 ;; esac
  exec dropdb --force -U "$POSTGRES_USER" "$target_db"
'
```

## 운영 준비

- `/umc-product/platform/argocd/preview-github-token`은 PR read-only PAT를 가진다.
- `/umc-product/preview/app-{db,jwt,oauth,storage,email}` source 경로는 prod/dev와 분리한다.
  `app-email`만 명시적 예외로 dev와 같은 nonprod SES 자격증명을 사용하며 prod sender는 공유하지 않는다.
- `/umc-product/preview/app-db`는 shared preview app password를 가진다.
- `/umc-product/preview/postgres-preview-secrets`는 preview 관리자 자격증명을 가진다.
- GHCR package는 public이고 Pod는 anonymous pull한다. `ghcr-pull` Secret을 만들거나 주입하지 않는다.
- ApplicationSet은 `DEMODAY_QR_BASE_URL=https://university.neordinary.com`을 주입한다.
  QR이 여는 웹 주소이며 기존 웹의 API 목적지를 PR API로 바꾸는 설정은 아니다.
  PR별 image·DB·URL 값 없이 공통 values만 활성화하면 안 된다.
- Preview 이메일은 Gmail SMTP가 아니라 SES다. 샌드박스 상태에서는 검증된 수신자만 사용할 수
  있으므로, dev의 Gmail 메일 테스트 성공을 Preview 이메일 준비 완료로 판단하지 않는다.
- preview namespace의 app/DB Secret과 argocd token ExternalSecret이 `Ready=True`인지 확인한다.
- preview namespace의 `*.university.neordinary.com` Certificate가 `letsencrypt-production`을 사용하고,
  최신 generation에서 `Ready=True`이며 `preview-wildcard-tls`가 존재하는지 확인한다.
- `preview` label 권한을 신뢰된 maintainer로 제한한다.
- fork PR에 label을 붙여도 backend image가 생성되지 않는지 실제로 확인한다.
- 3개 preview가 떠 있을 때 네 번째 preview의 Service가 quota에서 fail-closed되고 database를 만들지 않는지 확인한다.

## 알려진 tradeoff

한 namespace/PostgreSQL로 메모리를 아끼지만 PR 사이 credential과 DB trust boundary는 없다.
성공 이미지 연동은 빌드 실패 이미지의 선택을 막지만, 기동·Flyway 실패나 DB 변경을 되돌려 주지는 않는다.
배포 전략과 DB 복구 절차는 별도로 검증한다.
PostDelete hook은 정확한 PR database의 연결을 강제 종료하고 DB를 지운다. 테스트 결과를
보관해야 한다면 PR 종료나 label 제거 전에 별도로 저장해야 한다.

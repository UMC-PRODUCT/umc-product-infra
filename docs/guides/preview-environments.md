# PR 프리뷰

대상은 내부의 신뢰된 same-repository PR뿐이다. fork와 외부 PR 코드는 build, preview, deploy하지 않는다.
동시 상한은 3개다. URL은 `https://api-pr-<PR번호>.university.neordinary.com`이며 모바일 앱이 직접 호출한다.
현재 Deployment와 Ingress는 trusted image publish가 준비될 때까지 비활성이다.

## 사용 조건

PR은 다음 조건을 모두 만족해야 한다.

- `umc-product-server` 저장소에서 만든 branch다.
- target branch는 `develop`이다.
- 작성자는 `MEMBER`, `OWNER`, `COLLABORATOR` 중 하나다.
- maintainer가 head repository와 작성자 관계를 확인했다.
- maintainer가 `preview` label을 붙였다.
- 향후 backend trusted CI가 PR head SHA image를 GHCR에 push했다.
- `values-preview.yaml`의 `deployment.enabled` gate를 켰다.

backend workflow는 same-repository와 author association을 fail-closed로 검사한다.
ApplicationSet PR generator는 author association을 직접 필터링하지 못하고 `preview` label을 본다.
따라서 `preview` label은 신뢰 검토를 끝냈다는 보안 승인이다. fork나 외부 PR에 붙이지 않으며,
외부 작성자는 label을 직접 붙일 권한을 갖지 않는다.
trusted publish CI는 같은 12자리 head-SHA tag를 덮어쓰지 않는다. Preview는 registry digest를
Git에 동적으로 전달하지 않으므로 tag 불변성은 publish 권한 경계에 의존한다.

## 수명주기

```text
Preview 활성화 전 공용 TLS 준비
  -> Certificate: preview/preview-wildcard (*.university.neordinary.com)
  -> Secret: preview/preview-wildcard-tls

trusted PR + preview label
  -> backend image:<head SHA 12자리>
  -> ApplicationSet poll
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

ApplicationSet은 PR을 120초마다 확인한다. 이름은 Application `umc-product-preview-<PR번호>`,
workload `umc-product-server-pr-<PR번호>`, database `umc_product_pr<PR번호>`,
관측 service `preview-umc-product-pr<PR번호>`다.

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

활성 PR 27은 Route53에 `api-pr-27.university.neordinary.com` exact A record와 ExternalDNS TXT ownership
record를 가진다. Route53은 DNS만 제공하며 모바일 앱은 조회된 IDC public IPv4의 Traefik `443/TCP`로
직접 HTTPS 요청한다.
TLS는 `preview` namespace의 `*.university.neordinary.com` 공용 Certificate가 만든 `preview-wildcard-tls` Secret을 사용한다.
모바일 직접 호출 API에는 별도 앞단 로그인 정책을 적용하지 않는다.

Grafana public gate를 통과한 뒤 일반 maintainer는 Tailscale 없이
`https://grafana.university.neordinary.com`에 개인 `Viewer` 계정으로 로그인해
`preview-umc-product-pr<번호>`의 log, metric, trace를 본다. gate 전 local-only health 점검과
kubectl은 인프라 관리자만 사용한다. 자세한 경계는
[모니터링 접근 가이드](monitoring-access.md)를 따른다.

```bash
kubectl get application -n argocd umc-product-preview-27
kubectl get certificate -n preview preview-wildcard
kubectl get secret -n preview preview-wildcard-tls
kubectl get deployment/umc-product-server-pr-27 service/umc-product-server-pr-27 \
  job/umc-product-server-pr-27-createdb -n preview
kubectl port-forward -n preview deployment/umc-product-server-pr-27 \
  18080:8080 19090:9090
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
kubectl get job -n preview umc-product-server-pr-27-createdb
kubectl logs -n preview job/umc-product-server-pr-27-createdb
```

## database 삭제

PR close/label 제거 시 Argo CD가 Application 리소스를 prune한 뒤 `PostDelete` hook이
같은 `createDatabase.name`을 삭제한다. render/runtime은 `^umc_product_pr[0-9]+$`만 허용하고
`postgres`, template, bootstrap, prod, dev를 거부한다. cleanup은 접속/파일 시각을 추측하지 않는다.

dropdb Job은 연결을 강제 종료하지 않는다. 연결, Secret, DB 장애로 DROP이 실패하면 Application은
`DeletionError`로 남고, 실패 Job도 원인 확인용으로 남는다. 원인을 고친 뒤 Application 삭제를 재시도한다.

```bash
kubectl get application -n argocd umc-product-preview-27
kubectl get job -n preview umc-product-server-pr-27-dropdb
kubectl logs -n preview job/umc-product-server-pr-27-dropdb
```

수동 DROP은 hook 복구 불가 시만 인프라 관리자가 대상 이름을 재확인하고 preview bootstrap
database에 접속해 수행한다.

```bash
kubectl exec -it -n preview postgres-preview-0 -- sh -ceu '
  target_db=umc_product_pr27
  suffix="${target_db#umc_product_pr}"
  case "$suffix" in ""|*[!0-9]*) exit 64 ;; esac
  exec dropdb -U "$POSTGRES_USER" "$target_db"
'
```

## 운영 준비

- `/umc-product/platform/argocd/preview-github-token`은 PR read-only PAT를 가진다.
- `/umc-product/preview/app-{db,jwt,oauth,storage,email}` source 경로는 prod/dev와 분리한다.
  `app-email`만 명시적 예외로 dev와 같은 nonprod SES 자격증명을 사용하며 prod sender는 공유하지 않는다.
- `/umc-product/preview/app-db`는 shared preview app password를 가진다.
- `/umc-product/preview/postgres-preview-secrets`는 preview 관리자 자격증명을 가진다.
- GHCR package는 public이고 Pod는 anonymous pull한다. `ghcr-pull` Secret을 만들거나 주입하지 않는다.
- preview namespace의 app/DB Secret과 argocd token ExternalSecret이 `Ready=True`인지 확인한다.
- preview namespace의 `*.university.neordinary.com` Certificate가 `letsencrypt-production`을 사용하고,
  최신 generation에서 `Ready=True`이며 `preview-wildcard-tls`가 존재하는지 확인한다.
- `preview` label 권한을 신뢰된 maintainer로 제한한다.
- fork PR에 label을 붙여도 backend image가 생성되지 않는지 실제로 확인한다.
- 3개 preview가 떠 있을 때 네 번째 preview의 Service가 quota에서 fail-closed되고 database를 만들지 않는지 확인한다.

## 알려진 tradeoff

한 namespace/PostgreSQL로 메모리를 아끼지만 PR 사이 credential과 DB trust boundary는 없다.
SHA tag로 ApplicationSet이 즉시 프리뷰를 만들지만 prod/dev의 immutable digest 수준은 아니다.
PostDelete hook은 정확한 PR database만 지우며, 연결이 남으면 자동 정리보다 안전한 실패를 선택한다.

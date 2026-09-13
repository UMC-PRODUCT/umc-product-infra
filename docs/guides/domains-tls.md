# 도메인과 TLS 운영 계약

운영 도메인은 `university.neordinary.com`을 사용한다. 상위 `neordinary.com`에서
이 하위 도메인을 Route 53 public hosted zone으로 NS 위임한다. Route 53은
DNS만 제공하며 proxy·CDN·WAF가 아니므로, DNS를 조회한 클라이언트는 고정 IDC
public IPv4의 Traefik `443/tcp`로 직접 연결한다.

ExternalDNS는 GitOps가 소유한 Ingress의 exact A/TXT record를 Route 53에 만든다.
cert-manager는 Route 53 DNS-01로 Let's Encrypt에 도메인 소유권을 증명하고,
prod/dev의 host별 인증서와 Preview의 wildcard 인증서를 발급·갱신한다.

이 저장소는 목표 상태와 실행 절차만 선언한다. AWS stack 배포와
상위 `neordinary.com`의 NS 등록은 별도의 승인된 운영 작업이며 저장소 수정만으로 실행되지 않는다.

## 위임된 운영 도메인

`university.neordinary.com` public hosted zone은 Route 53 콘솔에서 만들고, 상위
`neordinary.com` 관리자에게 그 zone의 NS 4개 등록을 요청한다. `umc-product-route53-dns`
CloudFormation stack은 기존 Hosted Zone ID를 입력받아 controller용 IAM 사용자만 만들며,
Hosted Zone 자체는 생성하거나 삭제하지 않는다. NS·SOA authoritative 응답이 Route 53으로
수렴하기 전에 ExternalDNS, cert-manager, Ingress를 활성화하지 않는다.

apex frontend와 `admin`, `tech`처럼 Kubernetes가 소유하지 않는 record는 별도로 관리한다.
ExternalDNS는 API, Grafana와 Argo CD record만 소유한다.

## Host와 접근 경계

| 용도 | Host | 접근 경계 |
|---|---|---|
| prod API | `api.university.neordinary.com` | Route 53 A record → public direct HTTPS, API 인증 |
| dev API | `api-dev.university.neordinary.com` | Route 53 A record → public direct HTTPS, API 인증 |
| PR preview API | `api-pr-<PR>.university.neordinary.com` | Route 53 exact A record → public direct HTTPS, API 인증 |
| Grafana | `grafana.university.neordinary.com` | Route 53 A record → public direct HTTPS, Grafana 로그인 필수 |
| Argo CD | `argo.university.neordinary.com` | Route 53 A record → public direct HTTPS, Argo CD 로그인 필수 |

prod/dev/preview API는 모바일 앱이 직접 호출한다. Route 53은 요청을 대신 차단하지
않으므로 abuse 방어는 앱 인증, Traefik rate limit, 상위 방화벽과 로그 감시로
별도 구성한다. Grafana도 public HTTPS로 열지만 익명 접근과 회원가입은 끄고 운영자가
발급한 사람별 Grafana 계정만 사용한다. 기본 Organization role은 `Viewer`다.
Argo CD는 비공유 `admin`과 공용 조회 계정 `umc-viewer`를 사용한다. 익명 접근은 끄며,
로컬 계정에는 MFA나 GitHub 팀 제한이 없다. 계정 관리는 [Argo CD 로컬 계정](ansible-bootstrap.md#argo-cd-로컬-계정)을 따른다.

## 책임 분리

- DNS 운영자: Route 53 public hosted zone, registrar 또는 상위 zone의 NS 위임, 선택적 DNSSEC
- ExternalDNS: `external-dns.kubernetes.io/managed-by=umc-infra` annotation이 붙은 Ingress의 exact A record와 TXT ownership record
- cert-manager: API·Grafana·Argo CD host별 TLS Secret, Preview 공용 wildcard TLS Secret, Let's Encrypt 갱신
- Argo CD: controller, ClusterIssuer, Certificate, Ingress desired state
- 제공업체 방화벽과 UFW: 공인 SSH `22/tcp`와 HTTPS `443/tcp`를 허용하고 DB `5432/tcp`와 Kubernetes API `6443/tcp`는 비공개로 유지

wildcard DNS record는 만들지 않는다. ExternalDNS는 `api-pr-27.university.neordinary.com` 같은
exact record를 PR마다 만들고 PR 삭제 시 TXT ownership 범위에서 정리한다.
cert-manager는 `preview` namespace에 `Certificate/preview-wildcard` 하나를 두고
`*.university.neordinary.com`을 `preview-wildcard-tls` Secret으로 발급한다.

## Hosted Zone와 public IP gate

모든 Ingress의 `external-dns.kubernetes.io/target`은 같은 고정 IDC public IPv4다.
ExternalDNS의 `--target-net-filter=<IDC IPv4>/32`도 같은 값으로 제한한다.
Hosted Zone ID와 고정 public IPv4는 cert-manager, ExternalDNS와 Ingress 설정에 반영되어 있다.

- application chart `externalDNS.target`: `1.255.226.166`
- ExternalDNS controller `--target-net-filter`: `1.255.226.166/32`
- Grafana Ingress `external-dns.kubernetes.io/target`: `1.255.226.166`
- Argo CD Ingress `external-dns.kubernetes.io/target`: `1.255.226.166`

이 값은 새 Cafe24 IDC의 이관 대상이다. Git 설정만으로 실제 DNS 전환이나 외부 접속 검증이
완료된 것은 아니며, 전환 gate를 통과한 뒤 live A/TXT record와 HTTPS를 확인한다.

Hosted Zone ID와 public IPv4는 비밀값이 아니라 Git에 둔다. IP를 미리 넣어도 Ingress가 없으면
ExternalDNS가 A record를 만들지 않는다. `203.0.113.10` 같은 TEST-NET 주소는 CI의 합성 렌더
fixture에서만 사용하며 실제 desired state로 허용하지 않는다.

Route 53은 origin proxy가 아니므로 UFW와 Cafe24 IDC 상위 방화벽에서 public
`443/tcp`를 인터넷에 허용해야 한다. DNS-01을 쓰므로 Let's Encrypt용 public
`80/tcp`는 필요하지 않다. 별도 upstream proxy가 없는 구조에서 Traefik은 외부가
임의로 보낸 `X-Forwarded-*`를 신뢰하지 않아야 한다.

## Route 53 자격증명

cert-manager와 ExternalDNS는 자격증명을 공유하지 않는다. 각 IAM 사용자의 DNS 변경
권한은 해당 hosted zone ARN으로 제한한다. 키는 Git이나 manifest에 넣지 않고
AWS Secrets Manager에 저장한 뒤 ESO로 namespace별 Kubernetes Secret에 동기화한다.

| 소비자 | AWS Secrets Manager source | Kubernetes Secret |
|---|---|---|
| cert-manager | `/umc-product/platform/cert-manager/route53-credentials` | `cert-manager/route53-credentials` |
| ExternalDNS | `/umc-product/platform/external-dns/route53-credentials` | `external-dns/route53-credentials` |

두 source의 JSON property는 `access-key-id`, `secret-access-key`다. cert-manager에는 DNS-01
challenge TXT 작성과 변경 상태 조회, ExternalDNS에는 exact A/TXT record 조회·변경에
필요한 최소 권한만 준다.

## 최초 활성화 순서

1. Route53에 `university.neordinary.com` public hosted zone을 만들고 상위 zone에 NS 4개를 등록한다.
2. 외부 resolver에서 NS·SOA가 Route 53으로 수렴했는지 확인한다.
3. 기존 Hosted Zone ID를 전달해 `umc-product-route53-dns` IAM stack을 배포한다.
4. cert-manager·ExternalDNS 전용 IAM 자격증명을 만들고 각 Secrets Manager source에 저장한다.
5. ExternalDNS와 ClusterIssuer에 같은 Hosted Zone ID를 넣고, 모든 public IPv4 target과 `/32` filter가 일치하는지 확인한다.
6. `route53-credentials` ExternalSecret 두 개와 controller rollout을 확인한다.
7. production Certificate를 적용한다. 같은 Application의 Ingress도 함께 활성화했다면 Argo CD가
   Certificate의 최신 generation `Ready=True`를 기다린 뒤 다음 sync wave를 적용한다.
8. Grafana exact A/TXT record, 인증서 체인, 로그인 필수 상태와 팀원 `Viewer` 계정을
   [모니터링 접근 가이드](monitoring-access.md)대로 검증한다.
9. 첫 GHCR image tag·digest가 준비되면 prod/dev별 Deployment와 API Ingress를 같은 PR에서 켠다.
10. 외부에서 DNS 결과가 고정 IDC IPv4인지, HTTPS 인증서 체인과 API 인증이 정상인지 검증한다.
11. Preview는 production wildcard Certificate가 `Ready=True`인 것을 확인한 뒤 trusted PR에만 연다.

관리자 PC에서 DNS 위임을 확인한다.

```bash
dig +short NS university.neordinary.com
dig +short SOA university.neordinary.com
```

클러스터 명령은 개인 관리자 공인 SSH로 접속한 IDC에서 실행한다. kubeconfig를 PC로 복사하지 않는다.

```bash
sudo k3s kubectl wait --for=condition=Ready externalsecret/route53-credentials \
  -n cert-manager --timeout=180s
sudo k3s kubectl wait --for=condition=Ready externalsecret/route53-credentials \
  -n external-dns --timeout=180s
sudo k3s kubectl get clusterissuer
sudo k3s kubectl get certificate -A
sudo k3s kubectl get ingress -A
sudo k3s kubectl logs -n external-dns deployment/external-dns --since=10m
```

Secret 값은 출력하지 않는다. ClusterIssuer 실패는 cert-manager Challenge·event에서,
DNS 불일치는 ExternalDNS log, Ingress status, domain/zone filter, TXT owner에서 확인한다.

## Argo CD 공개 HTTPS

Argo CD core의 URL·HTTP backend·계정은 Ansible Helm release가 소유한다. 별도
`argocd-access` Application은 `argocd` namespace의 Certificate, Ingress와 server NetworkPolicy만
관리하며, 좁은 AppProject 권한으로 core ConfigMap이나 Secret을 수정하지 않는다.

1. Route 53 IAM에 `argo.university.neordinary.com` A record,
   `_external-dns.a-argo.university.neordinary.com` ownership TXT와
   `_acme-challenge.argo.university.neordinary.com` DNS-01 TXT의 exact 권한을 반영한다.
2. 기존 계정 로그인과 SSH 복구 경로를 확인한다. server NetworkPolicy가 먼저 생성된 것을 확인한 뒤
   검토한 core Helm values를 적용한다.
   다른 구성요소의 기존 NetworkPolicy는 유지한다. chart의 server 전체 허용 정책을 함께 남기면
   정책이 합집합으로 적용되어 좁은 허용 범위가 무력화되므로 제거되어야 한다.
3. `argocd-access` 안에서 Certificate와 NetworkPolicy는 wave `-2`, Ingress는 wave `1`이다.
   Certificate의 최신 generation이 `Ready=True`가 된 뒤에만 Ingress가 생성된다.
4. 외부 DNS가 `1.255.226.166`인지, HTTPS 인증서 체인과 admin/viewer 권한이 정상인지 확인한다.
   TLS는 Traefik의 `websecure`에서 종료하고, backend는 ClusterIP Service의 `http` port를 거쳐
   server `8080/TCP`로 연결한다. `server.insecure=true`는 이 내부 구간에만 해당하며 public HTTP를 열지 않는다.

공개 CLI 접속은 `argocd login argo.university.neordinary.com --grpc-web --username umc-viewer`를
사용하고 비밀번호는 대화형으로 입력한다. 공개 주소에 `--plaintext`나 인증서 검증 생략 옵션을 쓰지 않는다.
접속 중단 시 [SSH를 통한 복구 경로](ansible-bootstrap.md#argo-cd-공개-접속과-복구)를 사용한다.
긴급 공개 차단은 Git에서 Ingress만 제거하고, Certificate·NetworkPolicy·계정 Secret은 보존한다.

## 변경과 복구

- prod/dev host 변경 시 Certificate, Ingress host/TLS, ExternalDNS filter, 앱 public URL을 한 PR에서 바꾸고 DNS TTL을 고려한다.
- Preview host는 wildcard 범위 안에서 Ingress, exact record, public URL만 바꾸고 공용 Certificate는 PR 수명주기와 분리한다.
- 긴급 차단은 먼저 Git에서 Ingress를 비활성화한다. hosted zone이나 TLS Secret을 삭제하지 않는다.
- node IP 변경 시 모든 `externalDNS.target`과 ExternalDNS `--target-net-filter`를 한 PR에서 새 `/32`로 바꾸고 수렴을 확인한다. Route 53 console에서 ExternalDNS 소유 record를 고정하지 않는다.
- `enableCertificateOwnerRef=true`라 Certificate 삭제 시 TLS Secret도 정리된다. 실수로 삭제하면 Git을 복구하고 `Ready=True` 재발급 전까지 해당 Ingress를 열지 않는다.

ACME account 연락처 email은 운영 수신 주소 확정 전까지 생략한다. 이후 staging과
production ClusterIssuer에 같은 monitored email을 Git으로 추가한다.

## Route 53 access key 회전

cert-manager와 ExternalDNS access key는 하나씩 회전하며 동시에 폐기하지 않는다.

1. 같은 hosted zone 최소 권한으로 새 access key를 발급한다.
2. 해당 AWS Secrets Manager source에 새 version을 저장한다.
3. `route53-credentials` ExternalSecret을 force-sync하고 `Ready=True`와 새 refresh time을 확인한다.
4. cert-manager는 staging challenge 또는 선택한 Certificate 갱신, ExternalDNS는 controller restart 후 exact record reconcile로 새 key를 검증한다.
5. CloudTrail에서 이전 key 사용이 더 이상 없고 두 controller가 정상임을 확인한 뒤 이전 key를 비활성화·폐기한다.

다음 명령은 개인 관리자 공인 SSH로 접속한 IDC에서 실행한다.

```bash
namespace="${DNS_CREDENTIAL_NAMESPACE:?cert-manager 또는 external-dns를 지정하세요}"
case "$namespace" in cert-manager|external-dns) ;; *) exit 64 ;; esac
rotation_id="$(date +%s)"
sudo k3s kubectl annotate externalsecret/route53-credentials -n "$namespace" \
  external-secrets.io/force-sync="$rotation_id" --overwrite
sudo k3s kubectl wait --for=condition=Ready externalsecret/route53-credentials \
  -n "$namespace" --timeout=180s

if test "$namespace" = external-dns; then
  sudo k3s kubectl rollout restart deployment/external-dns -n external-dns
  sudo k3s kubectl rollout status deployment/external-dns -n external-dns --timeout=300s
  sudo k3s kubectl logs -n external-dns deployment/external-dns --since=10m
fi
```

Secret 값과 access key ID를 공유 로그에 남기지 않는다.

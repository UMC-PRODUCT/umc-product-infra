# 모니터링 접근

관측 stack의 구성과 파일 읽는 순서는
[관측 스택 배포 README](../../argocd/applications/platform/observability/README.md), dashboard와 alert
수정 방법은 [관측 원본 README](../../observability/README.md)를 먼저 본다.

Grafana의 canonical URL은 `https://grafana.university.neordinary.com`이며 UMC 팀원이 인터넷에서 접속한다.
VPN은 팀원의 Grafana 접속 조건이 아니다. 공개 HTTPS 뒤에서 Grafana 자체 로그인을 사용하고,
익명 접근과 자체 회원가입은 끈다. 운영자가 사람별 계정을 만들고 기본 권한은 `Viewer`로 둔다.

저장소 desired state에는 고정 IDC public IPv4, production Certificate와 public Ingress가 함께
선언되어 있다. Argo CD는 Grafana Application 안에서 Certificate wave `-2`가 최신 generation으로
`Ready=True`가 된 뒤 Ingress wave `1`을 적용한다. 실제 클러스터 수렴 여부는 아래 절차로 확인한다.

## 클러스터 내부 health 점검

인프라 관리자는 IDC 노드에서 cluster-local Grafana를 port-forward해 health를 점검할 수 있다.

```bash
sudo -n k3s kubectl port-forward -n monitoring service/grafana 13000:80
```

관리자는 공인 IP의 개인 `admin` SSH 계정을 사용한다. 관리자 PC의 SSH tunnel은 local bind만 연다.

```bash
ssh -L 13000:127.0.0.1:13000 "$IDC_SSH_USER@$IDC_NODE_HOST"
```

다른 terminal에서 canonical Host header로 health만 확인한다.

```bash
curl -fsS -H 'Host: grafana.university.neordinary.com' http://127.0.0.1:13000/api/health
```

canonical `https://grafana.university.neordinary.com`, domain enforcement, secure cookie 때문에 HTTP port-forward는 브라우저 로그인에
쓰지 않는다. 팀원 UI는 public gate 통과 후 canonical HTTPS URL에서만 연다.
`--address 0.0.0.0` port-forward나 `root_url`/cookie 설정 완화는 금지한다.

초기 관리자 계정은 `/umc-product/platform/monitoring/grafana-admin` source의 `admin-user`,
`admin-password`에서 온다. 값을 Git, shell argument, 채팅에 남기거나 `kubectl get -o yaml`로 출력하지 않는다.

## Dashboard 사용

어떤 화면을 열지 모르겠다면 [상황별 안내와 전체 대시보드 목록](../../observability/README.md#어떤-상황에-어떤-대시보드를-볼까)을 먼저 본다.

직접 관리하는 UMC dashboard 여덟 개는 `UMC Product` folder에 있다. `Kubernetes` folder에는
kube-prometheus-stack의 node·namespace·workload 기본 dashboard가 있다. 로그인 후 기본 홈으로 열리는
`UMC PRODUCT System Overview`에서 시작하고, 상단 link에서 같은 시간 범위와 변수를 유지한 채
상세 dashboard로 이동한다.

Application Detail의 Service 변수는 Loki label 목록이 아니라 Prometheus recording rule
`umc_product_service_fallback_info`의 `application` label에서 가져온다. 따라서 앱 signal이 잠시
비어도 기본 prod/dev/local 선택지가 유지된다. Overview는 이 목록에서 prod/dev만 비교한다.

상단 service 변수에는 아래 값이 표시된다.

| 값 | 환경 |
|---|---|
| `local-umc-product` | local |
| `prod-umc-product` | prod |
| `dev-umc-product` | dev |

preview 환경은 fallback recording rule의 고정 option이 아니다. 필요할 때 Service 변수의 custom value에
정확한 `application` 이름을 입력한다.

Loki query 예시다.

```logql
{service_name="preview-umc-product-pr27"}
{service_name="preview-umc-product-pr27"} |= "ERROR"
{service_name="preview-umc-product-pr27"} | json | taskId=`abc123`
```

trace ID link는 Tempo datasource를 연다. metrics, logs, traces의 service name은 `SPRING_APPLICATION_NAME`과 일치시킨다.

PostgreSQL Detail은 **production 전용**이며 두 source를 함께 쓴다. dev에는 PostgreSQL exporter와
해당 Kubernetes DB workload가 없으므로 빈 dev 패널로 해석하지 않는다.

- `postgres-exporter`: 접속 수, 최대 접속, DB 용량, transaction, cache hit, lock 같은 DB 내부 지표
- `kube-state-metrics`: `db` namespace의 PostgreSQL Pod/StatefulSet 상태, CPU·memory request/limit, PVC 요청 용량

이 PostgreSQL Detail 패널의 CPU·memory는 **Kubernetes에 선언한 예약/limit**이다.
Pod의 실제 CPU·memory 사용량은 kubelet/cAdvisor를 수집하는 `Kubernetes` folder의 workload
dashboard에서 namespace `db`로 조회한다. kube-state-metrics는 cluster 상태를 읽지만 Secret과
ConfigMap 내용은 수집하지 않는다.

`Kubernetes / Persistent Volumes`의 사용량 패널은 현재 `local-path`에서 각 PVC가 놓인
공용 파일시스템의 capacity·used·available 값을 보여준다. 여러 PVC에 비슷한 값이 표시될 수
있으며, 개별 PVC 디렉터리의 실제 점유량이나 요청 용량 대비 quota로 해석하지 않는다.
PVC의 선언된 요청 용량은 PostgreSQL Detail에서, 노드 전체 디스크 여유는 Node Exporter Host에서 확인한다.

Node Exporter Host의 OS 기본값은 실제 운영 target인 `Linux`다. `macOS`는 로컬 Homebrew
exporter를 연결해 확인할 때만 선택한다.

## 팀원 계정

public gate를 통과한 뒤 팀원은 VPN, SSH, kubeconfig 없이 canonical URL에 접속한다.
로그인 계정은 다음 원칙으로 관리한다.

- 관리자 계정을 공유하지 않고 사람마다 별도 local account를 만든다.
- 대시보드 조회가 목적이면 Organization role을 `Viewer`로 둔다.
- Dashboard 수정이 필요한 사람에게만 검토 후 `Editor`를 부여한다.
- `Admin`과 Server Admin은 계정·datasource 운영 담당자에게만 둔다.
- 퇴임·역할 변경 시 해당 사람의 계정을 비활성화하거나 삭제한다.
- `allow_sign_up: false`와 `auth.anonymous.enabled: false`를 유지한다.

초기에는 Grafana local account로 시작한다. 인원이 늘어 계정 회수가 어렵거나 조직 SSO·MFA가
필요해지면 OIDC 또는 별도 인증 proxy를 후속 변경으로 검토한다.

계정은 Server Admin으로 로그인해 `Administration` → `Users and access` → `Users` →
`New user`에서 만든다. 임시 password는 채팅방이나 Git에 남기지 않고 승인된 비밀 전달 수단으로
개인에게 보낸다. 생성 후 Grafana Admin은 `No`, 기본 organization role은 `Viewer`인지 확인한다.
Viewer도 연결된 datasource에 직접 query할 수 있으므로 제품팀 구성원에게만 계정을 발급하고,
애플리케이션 log에는 token, password, 개인정보를 남기지 않는다.

## public 배포 검증

Route53은 hostname을 IDC public IPv4로 해석할 뿐 proxy, WAF, 사용자 인증을 제공하지 않는다.
사용자 인증은 Grafana가 담당한다. Grafana Ingress는 API Ingress와 독립적으로 켜고 끌 수 있다.
아래 항목을 순서대로 확인한다.

1. Grafana target annotation이 Cafe24 IDC `1.255.226.166`이고 ExternalDNS `--target-net-filter`가 `1.255.226.166/32`인지 확인한다.
2. `monitoring` namespace의 `grafana.university.neordinary.com` production Certificate가 최신 generation에서 `Ready=True`인지 확인한다.
3. `root_url`, domain enforcement, secure cookie, basic auth, 익명 접근 off와 회원가입 off를 확인한다.
4. Ingress와 ExternalDNS의 exact A record 및 TXT ownership을 확인한다.
5. 외부에서 DNS 결과, certificate chain, HTTP→HTTPS 우회 불가와 login 화면을 확인한다.
6. 관리자 계정으로 로그인해 팀원별 `Viewer` 계정을 만들고 Viewer가 관리 기능에 접근하지 못하는지 확인한다.
7. 개인 관리자 공인 SSH 경유 local-only health와 제공업체 console 복구 경로도 계속 확인한다.

실제 IP와 production TLS 없이 hostname만 만들거나 임시 HTTP Ingress를 열지 않는다. TLS private
key와 AWS access key도 Git에 저장하지 않는다. DNS와 인증서 절차는
[도메인/TLS 가이드](domains-tls.md)를 따른다.

## 관측 경계

- 앱은 `otel-collector.monitoring.svc.cluster.local:4317/4318`에만 OTLP를 보낸다.
- Collector ingress는 `app`, `dev-app`, `preview` namespace의 앱 Pod만 허용한다.
- Grafana Ingress는 API Ingress와 독립적으로 관리한다. `kube-system`의 Traefik Pod에서 오는
  `3000/TCP`만 별도 허용한다.
- Actuator 9090은 application Service나 Ingress에 노출하지 않는다.
- Prometheus 14일 / 4GB 중 먼저 도달하는 조건, Tempo 7일, Loki 90일 retention을 사용한다.
- 단일 노드 local-path storage라서 node loss 시 telemetry가 유실될 수 있다.
- 같은 노드의 Alertmanager는 node/power/network 전체 장애를 알릴 수 없다. 운영 전 IDC 밖에 uptime monitor를 둔다.

Dashboard와 datasource는 Git에서 복원하지만 Grafana local user DB는 PVC에 있다. 노드 손실 후
관리자가 팀원별 계정을 다시 만들 수 있도록 승인 사용자 목록을 비밀값이 아닌 운영 기록으로 관리한다.

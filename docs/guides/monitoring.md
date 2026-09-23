# Grafana·Argo CD 조회 가이드

Grafana에서 앱·DB·서버 지표와 로그·trace를 보고, Argo CD에서 배포 상태와 Pod의 Events/Logs를
확인한다. 접속 주소와 로그인 정보는 인프라 담당자에게 승인된 비밀 전달 수단으로 받는다.
이 문서의 `https://<grafana-host>`, `https://<argo-host>`는 실제 접속 주소가 아닌 자리표시자다.

## 로그인하고 첫 화면 열기

### Grafana

1. 전달받은 Grafana HTTPS 주소를 브라우저에서 연다.
2. 본인에게 발급된 계정으로 로그인한다. 조회용 기본 권한은 `Viewer`다.
3. 기본 홈 `UMC PRODUCT System Overview`에서 문제가 발생한 시간 범위를 선택한다.
4. 상단 링크로 상세 dashboard에 이동한다. 같은 시간 범위와 변수가 유지된다.
   화면이 보이지 않으면 **Dashboards**에서 아래 표의 이름으로 검색한다.

계정 발급·초기 비밀번호·로그인 오류는 인프라 담당자에게 문의한다. 관리자 계정을 공유받거나
비밀번호를 Git·로그·공개 채팅에 남기지 않는다.

Grafana 기본 시간대와 이 저장소에서 직접 관리하는 대시보드 시간대는 `Asia/Seoul`(KST, UTC+9)이다.
기본 시간대는 개인·팀·조직의 명시적 설정을 덮어쓰지 않으며, Explore와 외부 대시보드에서는
시간 선택기에 표시된 시간대도 확인한다. 표시 시간대 변경은 저장된 로그·메트릭의 시각을
수정하지 않으므로 기존 데이터도 같은 시점을 기준으로 조회할 수 있다.
로그 본문의 `Z`로 끝나는 시각은 UTC이며 문자열 자체는 변환되지 않는다. 예를 들어
`2026-09-15T16:13:00Z`는 `2026-09-16 01:13:00 KST`와 같은 시각이다. 장애 공유 시 시간대를 함께 적는다.

### Argo CD

1. 전달받은 Argo CD HTTPS 주소를 브라우저에서 연다.
2. 승인된 조회 계정 `umc-viewer`로 로그인한다. 이 계정은 배포·설정 변경 권한이 없다.
3. **Applications**에서 확인할 앱을 열고 대상 namespace와 revision을 확인한다.
4. `Sync Status`와 `Health Status`를 함께 읽는다. `Synced`는 Git 선언과의 일치,
   `Healthy`는 리소스 health 판정이며 제품 API의 실제 요청 성공까지 보장하지 않는다.
5. 문제가 있는 리소스를 펼쳐 Pod의 **Events**와 **Logs**를 확인한다. Pending·재시작·OOM은
   발생 시각과 메시지를 기록하고 같은 시간대의 Grafana 지표·로그와 대조한다.

조회 중 발견한 문제는 앱·namespace·발생 시간·오류 메시지와 함께 담당자에게 전달한다.
캡처와 로그를 공유하기 전 token·password·개인정보가 포함됐는지 확인한다.

## 어떤 상황에 어떤 대시보드를 볼까?

먼저 `UMC PRODUCT System Overview`를 열고, 이상이 있는 부분만 아래 상세 화면으로 좁혀 본다.
아래 목록은 저장소가 배포하도록 선언한 dashboard 기준이다. 화면이나 데이터가 없으면 시간 범위와
변수를 확인한 뒤 담당자에게 문의한다.

### UMC Product 폴더 — 앱과 운영 DB

| 이런 상황이면 | 열 대시보드 | 먼저 볼 것 |
|---|---|---|
| 어디가 문제인지 모르겠다 | `UMC PRODUCT System Overview` | prod/dev 요청량·5xx 비율·응답 시간, 운영 DB·호스트 요약 |
| API가 느리거나 에러가 늘었다 | `Application Detail` | HTTP 응답 시간, JVM 메모리·GC, HikariCP(DB 연결 풀) 포화 |
| 구 API를 어떤 앱 버전이 아직 호출하는지 확인하고 싶다 | `API 호환성 · 사용 현황` | 후보 경로·메서드별 호출, 버전 누락, 최근 관측 시각. [사용 가이드](api-lifecycle.md) |
| 에러 메시지와 원인을 찾고 싶다 | `서버팀 로그 탐색` | 같은 시간대의 ERROR 로그와 trace ID |
| 특정 요청이 어디서 오래 걸렸는지 보고 싶다 | `API 처리 흐름 (Tempo)` | 요청 trace의 구간별 처리 시간. 로그의 trace ID로도 연결 가능 |
| GraphQL 요청만 느리다 | `UMC PRODUCT — GraphQL` | GraphQL 요청량·지연·오류 |
| 캐시가 잘 동작하는지 궁금하다 | `UMC PRODUCT — Cache` | 인메모리 Caffeine 캐시 hit/miss·적중률·항목 수. Redis 상태가 아님 |
| 운영 DB 연결이 부족하거나 DB가 느리다 | `PostgreSQL Detail — Production Only` | 접속 수/최대 접속, transaction·lock·DB 용량 |
| 서버 전체 CPU·메모리·디스크가 부족하다 | `Node Exporter Host Metrics` | Linux 호스트 전체 CPU·메모리·디스크·네트워크 |

앱 상세의 `Service`는 환경에 맞게 선택한다.

| 값 | 환경 |
|---|---|
| `prod-umc-product` | prod |
| `dev-umc-product` | dev |
| `local-umc-product` | local |

Service 목록은 앱 signal이 비어도 유지된다. `local-umc-product`가 보인다고 현재 클러스터에
로컬 앱이 배포됐다는 뜻은 아니다. Overview는 prod/dev만 비교한다. preview는 고정 선택지에
없으므로 필요하면 Service의 custom value에 정확한 `application` 이름을 입력한다.

### Kubernetes 폴더 — 클러스터와 Pod 자원

| 이런 상황이면 | 열 대시보드 | 먼저 볼 것 |
|---|---|---|
| 어떤 앱/Pod가 자원을 많이 쓰는가 | `Kubernetes / Compute Resources / Cluster` → `Namespace (Pods)` → `Pod` | 전체 → namespace → 개별 Pod의 실제 CPU·메모리, request/limit, CPU throttling(CPU 제한이 걸린 실행 주기의 비율) |
| 배포 단위나 노드끼리 비교하고 싶다 | `Kubernetes / Compute Resources / Workload`, `Nodes Overview` | Deployment/StatefulSet 등 workload별 사용량, 노드별 사용량 |
| 네트워크 송수신이 늘었다 | `Kubernetes / Networking / Cluster` → `Namespace (Pods)` → `Pod` | 범위를 좁혀 송수신량·패킷 drop 확인 |
| 클러스터 내부 DNS나 관리 API가 느리다 | `CoreDNS`, `Kubernetes / API server`, `Kubernetes / Kubelet` | DNS 질의, Kubernetes 관리 API, 노드의 Pod 관리 지표. 제품 API는 `Application Detail`에서 본다 |
| 저장 공간이 부족하다 | `Node Exporter Host Metrics`, `Kubernetes / Persistent Volumes` | 노드 디스크 여유, 볼륨이 사용하는 파일시스템 상태. 아래 PVC 주의점 참고 |
| 모니터링 자체가 정상인지 궁금하다 | `Prometheus / Overview`, `Alertmanager / Overview`, `Grafana Overview` | 수집·쿼리 처리, 알림 처리, 화면 서버 상태 |

표에서 줄여 쓴 `Namespace (Pods)`, `Pod`, `Nodes Overview` 등은 같은 앞부분을 가진 대시보드 이름이다.
`namespace`는 운영 API `app`, 개발 API `dev-app`, 운영 DB `db`, 개발 DB `dev-db`,
모니터링 `monitoring` 등으로 선택한다.
Pod가 Pending이거나 재시작/OOM이 발생한 원인은 Argo CD의 해당 Pod → Events/Logs도 확인한다.
기본 대시보드에는 이 상태를 전용으로 나열하는 패널이 없다.

<details>
<summary>Kubernetes 폴더 전체 목록 — 차트 87.21.0 기준 23개</summary>

- `Kubernetes / Compute Resources / Cluster`
- `Kubernetes / Compute Resources /  Multi-Cluster`
- `Kubernetes / Compute Resources / Namespace (Pods)`
- `Kubernetes / Compute Resources / Namespace (Workloads)`
- `Kubernetes / Compute Resources / Node (Pods)`
- `Kubernetes / Compute Resources / Nodes Overview`
- `Kubernetes / Compute Resources / Pod`
- `Kubernetes / Compute Resources / Workload`
- `Kubernetes / Networking / Cluster`
- `Kubernetes / Networking / Namespace (Pods)`
- `Kubernetes / Networking / Namespace (Workload)`
- `Kubernetes / Networking / Pod`
- `Kubernetes / Networking / Workload`
- `Kubernetes / API server`
- `Kubernetes / Kubelet`
- `Kubernetes / Persistent Volumes`
- `Node Exporter / USE Method / Cluster`
- `Node Exporter / USE Method / Node`
- `Node Exporter / Nodes`
- `CoreDNS`
- `Prometheus / Overview`
- `Alertmanager / Overview`
- `Grafana Overview`

Multi-Cluster는 여러 클러스터를 비교할 때 쓰는 화면이므로 현재 단일 클러스터에서는 먼저 볼 필요가 없다.

</details>

## 로그에서 요청 trace까지 찾기

1. `서버팀 로그 탐색`에서 서비스와 문제가 발생한 시간 범위를 선택한다.
2. Loki 쿼리로 에러 메시지나 요청 식별자를 좁힌다. 아래 서비스 이름과 `traceId`는 예시이므로
   실제 확인할 값으로 바꾼다.
3. 로그에 trace ID가 있으면 링크를 눌러 Tempo에서 요청의 구간별 처리 시간을 확인한다.
   `API 처리 흐름 (Tempo)`에서도 같은 서비스·시간 범위로 trace를 찾을 수 있다.

```logql
{service_name="prod-umc-product"}
{service_name="prod-umc-product"} |= "ERROR"
{service_name="prod-umc-product"} | traceId="원래_요청_TRACE_ID"
```

위 쿼리는 한 줄씩 실행한다. 요청 응답의 `X-Trace-Id` 또는 로그의 `traceId`로 조회한다.
OTLP로 수집한 속성은 위처럼 필터링하며, JSON 본문이 아닌 로그에 `| json`을 붙이지 않는다.
metrics·logs·traces의 service name은 앱의 `SPRING_APPLICATION_NAME`과 일치해야 한다.
trace가 없으면 시간 범위·서비스 이름·trace ID를 먼저 확인하고 수집 여부를 담당자에게 문의한다.

## 수치를 읽을 때 주의할 점

- 실제 사용량, request(예약량), limit(상한)은 서로 다르다. `JVM 가용 CPU`도 JVM이 인식한
  수이지 서버 전체 코어 수가 아니다. Pod 사용량은 Kubernetes의 Compute Resources 화면에서 확인한다.
- `No data`는 사용량 0이나 정상이라는 뜻이 아니다. 시간 범위·Service/namespace·데이터소스와
  해당 기능의 요청·수집 지표가 있는지 확인한다.
- Overview의 앱 신호는 메트릭이 들어오는지 보여준다. 외부 사용자의 HTTPS 접속 성공까지
  보장하지는 않는다.

### PostgreSQL·호스트·PVC

`PostgreSQL Detail — Production Only`는 production 전용이다. `postgres-exporter`의
접속 수·최대 접속·DB 용량·transaction·cache hit·lock과 `kube-state-metrics`의 `db` namespace
Pod/StatefulSet 상태·CPU/memory request/limit·PVC 요청 용량을 함께 보여준다.

DB 상세의 CPU·memory는 Kubernetes에 선언한 예약/limit이다. 실제 Pod 사용량은 Kubernetes
workload dashboard에서 namespace `db`로 조회한다. dev에는 PostgreSQL exporter를 배포하지
않으므로 dev DB workload는 namespace `dev-db`로 조회한다.

`Kubernetes / Persistent Volumes`의 사용량은 `local-path`에서 여러 PVC가 공유하는 파일시스템의
capacity·used·available 값일 수 있다. 개별 PVC 폴더의 실제 점유량이나 요청 용량 대비 quota로
해석하지 않는다. 선언된 요청 용량은 PostgreSQL Detail에서, 노드 전체 디스크 여유는
`Node Exporter Host Metrics`에서 확인한다.

호스트 화면은 같은 노드를 공유하는 모든 workload의 영향을 포함한다. OS 기본값은 운영 target인
`Linux`이며 `macOS`는 로컬 Homebrew exporter를 연결해 확인할 때만 선택한다.

## 관련 문서

- Preview 서비스 이름·상태 확인: [Preview 환경 사용](preview-environments.md)
- 계정 발급·접속 장애를 처리하는 운영자: [도구 접근 운영](../operations/tool-access.md)
- Dashboard·alert를 수정하는 담당자: [관측 원본 관리](../../observability/README.md)

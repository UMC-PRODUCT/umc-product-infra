# 모니터링 대시보드 사용과 원본 관리

접속: [Grafana](https://grafana.university.neordinary.com) — 팀원 계정으로 로그인한다.
계정 발급·접속 문제는 [모니터링 접근 가이드](../docs/guides/monitoring-access.md)를 본다.

## 어떤 상황에 어떤 대시보드를 볼까?

먼저 `UMC PRODUCT System Overview`를 열고, 시간 범위를 문제가 발생한 시각으로 맞춘다.
이상이 있는 부분만 아래 상세 화면으로 좁혀 본다. Grafana의 **Dashboards**에서 표의 이름으로 검색하면 된다.

### UMC Product 폴더 — 앱과 운영 DB

| 이런 상황이면 | 열 대시보드 | 먼저 볼 것 |
|---|---|---|
| 어디가 문제인지 모르겠다 | `UMC PRODUCT System Overview` | prod/dev 요청량·5xx 비율·응답 시간, 운영 DB·호스트 요약 |
| API가 느리거나 에러가 늘었다 | `Application Detail` | HTTP 응답 시간, JVM 메모리·GC, HikariCP(DB 연결 풀) 포화 |
| 에러 메시지와 원인을 찾고 싶다 | `서버팀 로그 탐색` | 같은 시간대의 ERROR 로그와 trace ID |
| 특정 요청이 어디서 오래 걸렸는지 보고 싶다 | `API 처리 흐름 (Tempo)` | 요청 trace의 구간별 처리 시간. 로그의 trace ID로도 연결 가능 |
| GraphQL 요청만 느리다 | `UMC PRODUCT — GraphQL` | GraphQL 요청량·지연·오류 |
| 캐시가 잘 동작하는지 궁금하다 | `UMC PRODUCT — Cache` | 인메모리 Caffeine 캐시 hit/miss·적중률·항목 수. Redis 상태가 아님 |
| 운영 DB 연결이 부족하거나 DB가 느리다 | `PostgreSQL Detail — Production Only` | 접속 수/최대 접속, transaction·lock·DB 용량 |
| 서버 전체 CPU·메모리·디스크가 부족하다 | `Node Exporter Host Metrics` | Linux 호스트 전체 CPU·메모리·디스크·네트워크 |

앱 상세의 `Service`는 `prod-umc-product` 또는 `dev-umc-product`를 선택한다.
`local-umc-product`는 선택지일 뿐, 현재 클러스터에 로컬 앱이 배포됐다는 뜻은 아니다.
DB 상세는 **production 전용**이고, 호스트 화면은 같은 노드를 공유하는 모든 workload의 영향을 포함한다.

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
`namespace`는 운영 API `app`, 개발 API `dev-app`, 운영 DB `db`, 모니터링 `monitoring` 등으로 선택한다.
Pod가 Pending이거나 재시작/OOM이 발생한 **원인**은 Argo CD의 해당 Pod → Events/Logs도 확인한다.
현재 기본 대시보드에는 이 상태를 전용으로 나열하는 패널이 없다.

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

### 수치를 읽을 때 주의할 점

- **실제 사용량 ≠ request(예약량) ≠ limit(상한)**이다. `JVM 가용 CPU`도 JVM이 인식한 수이지 서버 전체 코어 수가 아니다. Pod 사용량은 Kubernetes의 Compute Resources 화면에서 확인한다.
- `No data`는 사용량 0이나 정상이라는 뜻이 아니다. 시간 범위·Service/namespace·데이터소스를 먼저 확인하고, 해당 기능의 요청이나 수집 지표가 있는지 확인한다.
- 현재 `local-path`의 PVC 사용량은 여러 볼륨이 공유하는 파일시스템 값을 보여줄 수 있다. 개별 PVC 폴더의 점유량이나 강제 quota로 해석하지 않는다.
- Overview의 앱 신호는 **메트릭이 들어오는지**를 뜻한다. 외부 사용자의 HTTPS 접속 성공까지 보장하지는 않는다.

## 대시보드와 알림 원본 관리

이 디렉터리는 Grafana dashboard와 Prometheus·Loki alert rule의 **사람이 수정하는 원본**이다.
Kubernetes에 배포되는 YAML은 `scripts/gen-configmaps.py`가 생성한다. Dashboard와 Loki rule은
[`manifests/observability/`](../manifests/observability/)의 ConfigMap,
Prometheus rule은 [`manifests/observability-integrations/`](../manifests/observability-integrations/)의
`PrometheusRule`이다. Kubernetes 기본 dashboard·alert·recording rule은
`kube-prometheus-stack` chart가 소유한다.

> [!IMPORTANT]
> 두 디렉터리의 생성물을 직접 수정하지 않는다. 원본을 바꾸고 생성 스크립트를
> 다시 실행해야 다음 생성에서도 변경이 유지된다.

## 파일 구조

```text
observability/
├── dashboards/              # Grafana dashboard JSON 원본
└── rules/
    ├── prometheus-alerts.yaml
    └── loki-alerts.yaml

scripts/gen-configmaps.py             # 원본 → ConfigMap / PrometheusRule
manifests/observability/              # dashboard·Loki ConfigMap, NetworkPolicy
manifests/observability-integrations/ # ServiceMonitor, 생성된 PrometheusRule
```

관측 stack의 배포 구조는
[Argo CD observability README](../argocd/applications/platform/observability/README.md), Grafana 접속과
운영 절차는 [모니터링 접근 가이드](../docs/guides/monitoring-access.md)를 본다.

## Dashboard 배포 허용 목록

다음 UMC 원본 여덟 개를 Grafana sidecar가 읽는 ConfigMap으로 생성하며 `UMC Product` folder에 둔다.
Chart의 기본 dashboard는 별도 `Kubernetes` folder로 들어온다.

- `api-flow.json`
- `cache.json`
- `graphql.json`
- `node-exporter-host.json`
- `postgresql-detail.json` — production PostgreSQL exporter·Kubernetes 선언 자원
- `server-default.json` — 애플리케이션/JVM/HikariCP/로그 상세
- `server-logs.json`
- `system-overview.json` — prod/dev 앱 비교와 production 인프라 요약 landing

다음 원본은 저장소에 보관하지만 초기 배포에서는 제외한다.

- `github.json`
- `k6-load-test.json`

전용 plugin, token 또는 현재 K3s에 없는 datasource 의존성을 제거하고 검증한 뒤에만
[`DASHBOARD_ALLOWLIST`](../scripts/gen-configmaps.py)에 추가한다.

`postgresql-detail.json`은 AWS RDS/CloudWatch가 아니라 K3s 내부
`postgres_exporter`와 `kube-state-metrics`를 사용한다. CPU·memory request/limit와 PVC 요청
용량은 실제 사용량이 아닌 Kubernetes 선언값이며, 실제 호스트 사용량은 `node-exporter-host.json`에서
따로 확인한다. PostgreSQL과 Linux host exporter는 production 전용이다.

## Alert rule 변환

- `rules/prometheus-alerts.yaml`: K3s에 없는 target과 chart 기본 알림의 중복을 제외하고, 앱·DB·
  Collector·backup 상태용 rule을 `PrometheusRule`로 만든다. `monitoring` namespace와
  `release: prometheus` label이 Prometheus의 rule selector와 일치해야 한다.
- `rules/loki-alerts.yaml`: UMC log group 계약을 검사한 뒤 ConfigMap으로 만든다.

원본의 필수 group이나 변환 대상 alert 이름이 사라지면 생성 스크립트가 실패한다. 원본을
바꿀 때는 생성 결과뿐 아니라 제외·추가되는 rule도 함께 검토한다.

## 변경 순서

모든 명령은 저장소 루트에서 실행한다.

1. `observability/dashboards/` 또는 `observability/rules/`의 원본을 수정한다.
2. ConfigMap과 PrometheusRule을 다시 생성한다.
3. 원본과 생성물 diff를 함께 검토한다.
4. drift 검사와 전체 저장소 검증을 실행한다.

```bash
python3 scripts/gen-configmaps.py

git diff -- observability manifests/observability manifests/observability-integrations

python3 scripts/gen-configmaps.py --check
./scripts/validate.sh
```

## PR 확인 항목

- [ ] 원본과 생성된 ConfigMap·PrometheusRule을 같은 PR에 포함
- [ ] dashboard datasource UID와 service 변수 유지
- [ ] alert expression이 현재 scrape·OTLP label과 일치
- [ ] secret, token, 실제 사용자 정보가 JSON·annotation·panel text에 없음
- [ ] `python3 scripts/gen-configmaps.py --check` 성공
- [ ] `./scripts/validate.sh` 성공하며 필요한 검사 도구가 skip되지 않음

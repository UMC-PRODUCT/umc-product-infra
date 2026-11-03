# 관측 스택 배포 구조

이 디렉터리는 prod, dev, preview가 공유하는 관측 스택의 Argo CD Application을 선언한다.
여기서는 **무엇이 어떤 순서로 배포되는지**를 설명한다.

Dashboard와 alert 원본을 수정하려면 [원본 관리 README](../../../../observability/README.md),
팀원 조회 방법은 [Grafana·Argo CD 사용](../../../../docs/guides/monitoring.md),
계정·외부 공개 운영은 [도구 접근 관리](../../../../docs/operations/tool-access.md)를 본다.

## 신호 흐름

```text
애플리케이션
  → OTel Collector : OTLP metrics·logs·traces
      ├─ metrics → Prometheus → alert rule → Alertmanager → Discord
      ├─ logs    ┬ 운영·dev → Loki
      │          └ Preview  → WARN 이상 약 10% 표본 → Loki (push·queue 분리)
      └─ traces  → Tempo

Grafana
  ├─ Prometheus 조회
  ├─ Loki 조회
  └─ Tempo 조회
```

Collector는 애플리케이션이 보낸 세 신호를 각 저장소로 전달하는 중앙 gateway다. metrics·traces와
운영·dev 로그에는 sampler를 두지 않는다.

Preview 로그는 운영·dev와 다른 경로로 보낸다. Loki는 모든 환경이 한 tenant의 수집 상한을 함께 쓰고,
상한을 넘는 push를 통째로 429로 거부한다. 그래서 Collector는 `service.name`이 `preview-umc-product-pr<번호>`인
로그를 `routing/logs` connector로 `logs/preview` pipeline에 보낸다. 이 pipeline은 자기 batch와 32MiB queue를
가진 `otlphttp/loki-preview` exporter로 보내므로 Preview push에는 운영·dev 로그가 들어가지 않는다. 이 push
분리가 운영 로그를 지키는 주된 장치다.

Preview 로그는 양도 줄인다. INFO는 버리고 WARN 이상만 trace 단위로 약 10% 남긴다. 상한이 아니라 비율이므로
폭주량에 비례해 줄 뿐이다. Loki가 stream별 상한으로 Preview push 일부를 거부할 때 재시도 양을 줄이는 역할이다.
trace가 없는 로그(기동 실패 등)는 표본 추출 없이 보낸다. Preview의 요청별 원인은 Tempo trace에 전부 남는다.

이 때문에 Grafana에서 Preview 앱을 고르면 Loki의 `api_request_completed`(INFO 줄)를 세는 API 사용량 패널은
실제보다 적게 나온다. Preview의 요청 수와 지연은 Tempo trace와 Prometheus metric으로 본다.

## 파일을 읽는 순서

1. [`prometheus.yaml`](prometheus.yaml): kube-prometheus-stack 버전, Operator·CRD, 수집·알림·권한
2. [`integrations.yaml`](integrations.yaml): UMC ServiceMonitor와 생성된 PrometheusRule
3. [`config.yaml`](config.yaml): NetworkPolicy, UMC dashboard와 Loki rule ConfigMap
4. [`otel-collector.yaml`](otel-collector.yaml): 앱 신호를 받는 입구와 exporter
5. [`loki.yaml`](loki.yaml), [`tempo.yaml`](tempo.yaml): log와 trace 저장
6. [`grafana.yaml`](grafana.yaml): 기존 Grafana의 datasource, dashboard와 외부 접근

## 배포 순서와 수집 범위

`monitoring-config`는 wave 1, 관측 chart는 wave 2, `monitoring-integrations`는 wave 3이다.
CRD와 Operator가 먼저 준비된 다음 ServiceMonitor·PrometheusRule을 적용한다. 기존 standalone
Prometheus 전환은 [마이그레이션 runbook](../../../../docs/runbooks/monitoring-operator-migration.md)을 따른다.

`prometheus` Application은 `kube-prometheus-stack` chart 하나로 Prometheus Operator, CRD,
Prometheus, Alertmanager, node-exporter, kube-state-metrics와 Kubernetes 기본 dashboard·rule을
설치한다. Grafana chart는 여기서 끄고 기존 `grafana` Application을 유지한다. 기본 dashboard는
`Kubernetes`, UMC dashboard는 `UMC Product` folder에 표시한다.

Operator가 `monitoring` 안의 Prometheus·Alertmanager custom resource를 읽어 StatefulSet을 만든다.
Prometheus는 같은 namespace의 `release: prometheus` monitor·rule만 선택하며 ServiceMonitor가
대상 namespace의 Service label과 이름 있는 port를 선택한다. kubelet/cAdvisor와 kube-state-metrics로
클러스터 workload 사용량과 상태를 수집한다. SQLite K3s에 없는 etcd·scheduler·controller-manager·
kube-proxy 독립 endpoint와 관련 rule은 끈다.

## 권한과 알림 설정

Operator의 쓰기 권한은 `monitoring` Role에만 있고 cluster 권한은 node·namespace·storageclass
조회뿐이다. Prometheus와 kube-state-metrics의 cluster 권한도 명시적인 읽기 전용 목록을 쓴다.
AppProject는 CRD·ClusterRole·ClusterRoleBinding 세 종류만 허용하며 admission webhook은 끈다.

Discord 라우팅은 같은 Application의 `AlertmanagerConfig/prometheus-discord`가 소유한다.
`alertmanagerConfiguration`이 이를 global configuration으로 참조하므로 앱·DB namespace의
알림도 함께 처리한다. Operator는 `alertmanager-discord` Secret의 `discord-webhook` key를
SecretKeySelector로 읽어 runtime 설정을 생성한다. `useExistingSecret: true`는 이 native 구성과
충돌하는 chart 기본 설정 Secret 생성을 끄는 값이며, 별도 수동 설정 Secret을 만들라는 뜻이 아니다.

## Application과 보존 기간

| Application | 역할 | 보존 |
|---|---|---|
| `monitoring-config` | NetworkPolicy, UMC dashboard와 Loki rule ConfigMap | 해당 없음 |
| `monitoring-integrations` | UMC ServiceMonitor·PrometheusRule | 해당 없음 |
| `otel-collector` | 중앙 OTLP gateway | 저장하지 않음 |
| `prometheus` | Operator·CRD, metrics, Alertmanager, node-exporter, kube-state-metrics | 14일 / 4GB 중 먼저 도달하는 조건 |
| `loki` | logs와 Loki ruler | 운영 90일 (`2160h`), dev 30일, Preview 7일 |
| `tempo` | traces | 7일 (`168h`) |
| `grafana` | datasource와 dashboard 조회 | Git에서 dashboard 복원 |

## 저장 공간과 한계

| 구성 요소 | PVC | 주로 보관하는 것 |
|---|---:|---|
| Prometheus | 10Gi | metrics |
| Alertmanager | 1Gi | 알림 상태 |
| Loki | 5Gi | logs |
| Tempo | 50Gi | traces |
| Grafana | 1Gi | local user와 Grafana 내부 DB |

`local-path`는 PVC 요청량을 실제 disk quota로 강제하지 않으므로 Prometheus는 별도 4GB retention
상한을 둔다. Loki는 디스크 크기가 아니라 수집 속도를 묶는다. 모든 환경을 합쳐 4MB/s(burst 16MB)를
넘는 push는 429로 통째로 거부되고 `LokiDiscardedSamples` 알림이 뜬다. Collector가 모든 환경의 로그를
한 push로 보내므로 Preview 폭주만으로 운영 로그도 거부될 수 있다. Preview를 격리하려면 Collector에서
파이프라인을 나누거나 샘플링해야 한다. stream 상한 2MB/s는 1536KB/s를 넘으면 자동 분할되는 shard마다
걸리므로 환경별 상한이 아니다. 4MB/s가 이어지면 디스크는 계속 차고 보존기간은 오래된 데이터만 지우므로
노드 디스크 사용량은 따로 감시한다. 64KB를 넘는 줄은 잘라서 저장한다.
모든 PVC는 단일 노드의 `local-path`를 사용한다. Node나 disk를 잃으면 telemetry와 Grafana local
user 상태를 잃을 수 있다. Dashboard와 datasource는 Git에서 복원하지만 이 저장소는 PostgreSQL
backup을 대신하지 않는다.

같은 Node의 Prometheus와 Alertmanager는 Node·전원·회선 전체 장애를 스스로 알릴 수 없다.
운영 전에는 클러스터 밖 uptime monitor와 heartbeat를 별도로 둔다.

## 네트워크와 외부 접근

`monitoring` namespace는 ingress default-deny다. 앱의 OTLP `4317/4318`은 `app`, `dev-app`,
`preview` namespace에서 Collector로 오는 트래픽만 허용한다. Prometheus scrape와 Grafana
datasource 같은 수신 경로도 필요한 monitoring 내부 통신만 연다. egress default-deny는 아직
적용하지 않았으므로 outbound 제한은 별도 보강 항목이다.

Grafana는 승인된 host의 public HTTPS와 자체 로그인을 사용한다.
익명 접근과 회원가입은 끄고 팀원마다 기본 `Viewer` 계정을 발급한다. desired state의 production
Certificate와 public Ingress는 같은 Grafana Application에 있으며, Argo CD가 Certificate 준비를
기다린 뒤 Ingress를 적용한다. 활성화 검증과 계정 운영은
[모니터링 접근 가이드](../../../../docs/operations/tool-access.md)를 따른다.

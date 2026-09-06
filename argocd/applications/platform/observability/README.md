# 관측 스택 배포 구조

이 디렉터리는 prod, dev, preview가 공유하는 관측 스택의 Argo CD Application을 선언한다.
여기서는 **무엇이 어떤 순서로 배포되는지**를 설명한다.

Dashboard와 alert 원본을 수정하려면 [원본 관리 README](../../../../observability/README.md),
Grafana 접속·외부 공개·운영 절차는 [모니터링 접근 가이드](../../../../docs/guides/monitoring-access.md)를
본다.

## 신호 흐름

```text
애플리케이션
  → OTel Collector : OTLP metrics·logs·traces
      ├─ metrics → Prometheus → alert rule → Alertmanager → Discord
      ├─ logs    → Loki
      └─ traces  → Tempo

Grafana
  ├─ Prometheus 조회
  ├─ Loki 조회
  └─ Tempo 조회
```

Collector에는 별도 sampler를 두지 않는다. 애플리케이션이 보낸 세 신호를 각 저장소로 전달하는
중앙 gateway 역할만 한다.

## 파일을 읽는 순서

1. [`config.yaml`](config.yaml): NetworkPolicy와 생성된 dashboard·rule ConfigMap
2. [`otel-collector.yaml`](otel-collector.yaml): 앱 신호를 받는 입구와 exporter
3. [`prometheus.yaml`](prometheus.yaml): metrics, alert rule, Alertmanager와 node 상태
4. [`loki.yaml`](loki.yaml): log 저장과 Loki ruler
5. [`tempo.yaml`](tempo.yaml): trace 저장
6. [`grafana.yaml`](grafana.yaml): datasource, dashboard와 외부 접근 gate

`monitoring-config`는 sync wave 1에서 정책과 설정을 먼저 만들고, 나머지 workload는 wave 2에서
배포한다.

## Application과 보존 기간

| Application | 역할 | 보존 |
|---|---|---|
| `monitoring-config` | NetworkPolicy, dashboard와 rule ConfigMap | 해당 없음 |
| `otel-collector` | 중앙 OTLP gateway | 저장하지 않음 |
| `prometheus` | metrics, Alertmanager, node-exporter, kube-state-metrics | 14일 |
| `loki` | logs와 Loki ruler | 90일 (`2160h`) |
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

Grafana의 목표 접근 경로는 `grafana.university.neordinary.com` public HTTPS + Grafana login이다.
익명 접근과 회원가입은 끄고 팀원마다 기본 `Viewer` 계정을 발급한다. desired state의 production
Certificate와 public Ingress는 같은 Grafana Application에 있으며, Argo CD가 Certificate 준비를
기다린 뒤 Ingress를 적용한다. 활성화 검증과 계정 운영은
[모니터링 접근 가이드](../../../../docs/guides/monitoring-access.md)를 따른다.

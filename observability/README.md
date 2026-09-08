# 대시보드와 알림 원본 관리

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

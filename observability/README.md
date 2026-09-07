# 대시보드와 알림 원본 관리

이 디렉터리는 Grafana dashboard와 Prometheus·Loki alert rule의 **사람이 수정하는 원본**이다.
Kubernetes에 배포되는 YAML은 `scripts/gen-configmaps.py`가
[`manifests/observability/`](../manifests/observability/)에 생성한다.

> [!IMPORTANT]
> `manifests/observability/`의 생성물을 직접 수정하지 않는다. 원본을 바꾸고 생성 스크립트를
> 다시 실행해야 다음 생성에서도 변경이 유지된다.

## 파일 구조

```text
observability/
├── dashboards/              # Grafana dashboard JSON 원본
└── rules/
    ├── prometheus-alerts.yaml
    └── loki-alerts.yaml

scripts/gen-configmaps.py     # 원본 → Kubernetes ConfigMap
manifests/observability/      # 생성 결과와 NetworkPolicy
```

관측 stack의 배포 구조는
[Argo CD observability README](../argocd/applications/platform/observability/README.md), Grafana 접속과
운영 절차는 [모니터링 접근 가이드](../docs/guides/monitoring-access.md)를 본다.

## Dashboard 배포 허용 목록

다음 여섯 개만 Grafana sidecar가 읽는 ConfigMap으로 생성한다.

- `api-flow.json`
- `cache.json`
- `graphql.json`
- `node-exporter-host.json`
- `server-default.json` — 애플리케이션/JVM/로그와 K3s PostgreSQL exporter·선언 자원
- `server-logs.json`

다음 원본은 저장소에 보관하지만 초기 배포에서는 제외한다.

- `github.json`
- `k6-load-test.json`

전용 plugin, token 또는 현재 K3s에 없는 datasource 의존성을 제거하고 검증한 뒤에만
[`DASHBOARD_ALLOWLIST`](../scripts/gen-configmaps.py)에 추가한다.

`server-default.json`의 PostgreSQL 구간은 AWS RDS/CloudWatch가 아니라 K3s 내부
`postgres_exporter`와 `kube-state-metrics`를 사용한다. CPU·memory request/limit와 PVC 요청
용량은 실제 사용량이 아닌 Kubernetes 선언값이며, 실제 호스트 사용량은 `node-exporter-host.json`에서
따로 확인한다.

## Alert rule 변환

- `rules/prometheus-alerts.yaml`: K3s에 실제 scrape target이 없는 rule을 제외하고, 내부
  target·Collector·backup 상태용 rule을 추가해 ConfigMap으로 만든다.
- `rules/loki-alerts.yaml`: UMC log group 계약을 검사한 뒤 ConfigMap으로 만든다.

원본의 필수 group이나 변환 대상 alert 이름이 사라지면 생성 스크립트가 실패한다. 원본을
바꿀 때는 생성 결과뿐 아니라 제외·추가되는 rule도 함께 검토한다.

## 변경 순서

모든 명령은 저장소 루트에서 실행한다.

1. `observability/dashboards/` 또는 `observability/rules/`의 원본을 수정한다.
2. ConfigMap을 다시 생성한다.
3. 원본과 생성물 diff를 함께 검토한다.
4. drift 검사와 전체 저장소 검증을 실행한다.

```bash
python3 scripts/gen-configmaps.py

git diff -- observability manifests/observability

python3 scripts/gen-configmaps.py --check
./scripts/validate.sh
```

## PR 확인 항목

- [ ] 원본과 생성된 ConfigMap을 같은 PR에 포함
- [ ] dashboard datasource UID와 service 변수 유지
- [ ] alert expression이 현재 scrape·OTLP label과 일치
- [ ] secret, token, 실제 사용자 정보가 JSON·annotation·panel text에 없음
- [ ] `python3 scripts/gen-configmaps.py --check` 성공
- [ ] `./scripts/validate.sh` 성공하며 필요한 검사 도구가 skip되지 않음

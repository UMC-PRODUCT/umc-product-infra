# 관측 대시보드·알림 원본 관리

이 문서는 dashboard·alert 원본과 생성물을 관리하는 담당자용이다.
로그인·상황별 dashboard 선택·로그와 trace 조회는
[Grafana·Argo CD 조회 가이드](../docs/guides/monitoring.md)를 본다.

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
운영 절차는 [도구 접근 운영](../docs/operations/tool-access.md)을 본다.

## 원본을 읽는 순서

Dashboard JSON은 주석을 지원하지 않으므로 `title`과 `description`으로 화면의 패널을 찾는다.
`apiVersion: dashboard.grafana.app/v2`인 원본은 다음 순서로 읽으면 된다.

1. `metadata.name`과 `spec.description`: 대시보드 식별자와 목적. 식별자를 바꾸면 기존 링크도 확인한다.
2. `spec.variables`: 상단 필터의 선택지와 기본값. Service·Instance가 어떤 label을 조회하는지 확인한다.
3. `spec.elements`의 패널: `data.spec.queries` 아래 datasource와 `expr`/`query`가 실제 조회 조건이다.
4. 패널의 `vizConfig`와 `spec.layout`: 조회 결과의 단위·표시 방식과 배치다. 숫자를 바꾸기 전에 쿼리 단위와 맞는지 확인한다.

`cache.json`, `graphql.json`, `k6-load-test.json`은 기존 형식이다. 각각 `uid`,
`templating.list`, `panels[].targets`, `panels[].gridPos`에서 같은 역할을 찾는다.

알림 YAML은 `expr`(발동 조건) → `for`(조건 유지 시간) → `labels`(분류·라우팅) →
`annotations`(담당자에게 보낼 설명) 순서로 읽는다. `for: 0m`은 대기 없이 평가 결과를 반영한다는
뜻이며 메일·Discord 도착 시간을 보장하지 않는다. `record`는 계산 결과를 새 메트릭으로 저장하는
규칙이지 알림이 아니다. 특히 `umc_product_service_fallback_info`의 고정 선택지는 앱 생존 여부와 무관하다.

Prometheus 규칙의 `api`는 요청·지연, `database-cache`는 HikariCP·캐시,
`observability`는 수집·전달 실패를 다룬다. 원본에 있는 규칙이 모두 배포되는 것은 아니므로 아래
변환 규칙을 함께 확인한다. Loki는 일반 문자열 로그와 JSON 구조화 로그를 구분해 조회한다.
예외 이름이 잡혔다는 것만으로 원인을 단정하지 말고 해당 로그와 trace를 함께 본다.

알림 원본은 주석도 생성물에 복사된다. 원본 주석을 수정할 때도 아래 생성·검증 절차를 따른다.

## Dashboard 배포 허용 목록

다음 UMC 원본 아홉 개를 Grafana sidecar가 읽는 ConfigMap으로 생성하며 `UMC Product` folder에 둔다.
Chart의 기본 dashboard는 별도 `Kubernetes` folder로 들어온다.

Application Detail의 Service 변수는 Loki label 목록이 아닌 Prometheus recording rule
`umc_product_service_fallback_info`의 `application` label을 사용한다. 앱 signal이 잠시 비어도
prod/dev/local 선택지를 유지하며 Overview는 prod/dev만 비교한다. preview는 고정 fallback
option이 아니다. 원본 변경 시 이 변수와 dashboard 간 링크를 함께 확인한다.

- `api-flow.json`
- `api-lifecycle.json` — API·클라이언트 버전별 호출과 계측 누락 현황
- `cache.json`
- `graphql.json`
- `node-exporter-host.json`
- `postgresql-detail.json` — production PostgreSQL exporter·Kubernetes 선언 자원
- `server-default.json` — 애플리케이션/JVM/HikariCP/로그 상세
- `server-logs.json`
- `system-overview.json` — prod/dev 앱 비교와 production 인프라 요약 landing

`api-lifecycle.json`은 native OTLP structured metadata를 직접 조회한다. `clientVersion`은
인덱스 label이나 Prometheus label로 추가하지 않는다. 조회 비용을 줄이기 위해 1/7/14/30일
선택지를 제공하고 자동 새로고침을 끈다. No data를 0으로 대체하거나 미관측 API를 삭제 가능으로
표시하지 않는다. 조회·제거 검토 절차는 [API 호환성 사용 가이드](../docs/guides/api-lifecycle.md)를 본다.

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

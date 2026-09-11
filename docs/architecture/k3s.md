# 단일 IDC k3s 아키텍처 결정 기록

상태: bootstrap 전.

이 문서는 왜 이 구조를 선택했는지와 재검토 조건을 기록한다. 실행 순서는 [README](../../README.md), 위험 작업은 관련 guide와 [runbooks](../../runbooks/)가 기준이다.

## 전제

- Ubuntu x86_64 물리 서버 한 대를 사용한다.
- prod, dev, 선택된 PR preview와 관측 스택을 같은 클러스터에 둔다.
- 짧은 중단과 단일 노드 장애는 수용한다. 고가용성은 이번 범위가 아니다.
- 기존 서비스는 내려가 있어 in-place migration보다 새 bootstrap을 우선한다.
- Terraform은 사용하지 않는다.
- `umc-product-server` 저장소는 이번 변경에서 수정하지 않는다.

## 결정

| ID | 결정 | 이유 | 재검토 조건 |
|---|---|---|---|
| D1 | 단일 노드 k3s | 규모와 운영 인력에 맞는 가장 작은 운영 단위 | 노드 장애 허용이 어려워지거나 용량이 부족할 때 multi-node로 전환 |
| D2 | Ansible은 host/bootstrap, Argo CD는 cluster desired state 담당 | OS 변경과 Kubernetes reconciliation의 책임 분리 | 두 번째 cluster부터 inventory/cluster 계층 추가 |
| D3 | app-of-apps + 환경별 AppProject | source, namespace, cluster-scope 권한 제한 | 프로젝트 수보다 운영 복잡성이 커질 때 재구성 |
| D4 | root `prune: false`로 시작 | 초기 이름/경로 이관 중 자식 Application 오삭제 방지 | 첫 운영 안정화·복구 연습 뒤 `true` 검토 |
| D5 | 앱은 공통 Helm chart + 환경 values | 동일 runtime contract를 유지하면서 값만 분리 | 앱 종류가 둘 이상 생기면 chart별 디렉터리 추가 |
| D6 | 앱은 초기 replica 1 | local cache, in-memory WebSocket broker, scheduler의 다중 replica 안전성이 미확인 | relay/locking/cache invalidation 검증 후 확장 |
| D7 | PostgreSQL 18 + PostGIS 3.6 raw manifests | geometry/GiST migration이 있어 plain PostgreSQL 불가; 단일 DB에 operator는 과함 | HA/자동 failover가 필요할 때 CloudNativePG 검토 |
| D8 | prod/dev 전용 DB, preview 공유 DB Pod + PR별 database | isolation과 단일 노드 자원 사용의 균형 | preview 간 강한 보안 경계가 필요하면 namespace/DB 분리 |
| D9 | 앱 role은 최소권한, extension은 관리자 Job이 선설치 | Flyway가 superuser가 되는 것을 방지 | 별도 migration role 도입 시 기본 권한까지 재설계 |
| D10 | AWS Secrets Manager + namespaced SecretStore | Git에는 값 없이 계약과 IAM 경계만 유지 | AWS 의존을 없앨 때 SOPS/age 또는 Vault로 교체 |
| D11 | CloudFormation은 외부 IAM/S3/SES에만 사용 | Terraform 없이도 AWS 외부 의존성을 반복 가능하게 생성 | AWS 외부 의존성을 없애면 `cloud/aws` 제거 |
| D12 | 사용자 namespace default-deny + PSA restricted | lateral movement와 과도한 Pod 권한 축소 | FQDN egress 통제가 필요하면 Cilium/egress proxy 검토 |
| D13 | management 9090은 cluster 내부 전용 | health 상세 endpoint 외부 노출 방지 | 별도 authenticated management plane 도입 시 변경 |
| D14 | OTel Collector 단일 진입점 | 앱은 exporter backend를 몰라도 metrics/logs/traces 전송 | signal volume이 커지면 sampling/queue/storage 분리 |
| D15 | Prometheus 14d, Tempo 7d, Loki 90d | 초기 진단 가치와 단일 노드 disk 비용의 균형 | disk pressure 또는 규정 요구에 따라 조정 |
| D16 | 관측 PVC는 local-path | 가장 단순한 초기 구성 | telemetry 내구성이 중요해지면 object storage 이전 |
| D17 | Ingress는 edge 준비 전 기본 off | host가 확정돼도 인증서와 접근 정책 검증 전 외부 노출 방지 | Certificate Ready와 Route 53·direct HTTPS gate 검증 후 환경별 활성화 |
| D18 | public GHCR을 익명 pull하고 prod/dev는 digest, Preview는 write-once head-SHA tag 고정 | credential 배포 없이 운영 불변성과 동적 PR image 선택을 함께 확보 | package를 private로 바꾸거나 registry admission을 도입할 때 재설계 |
| D19 | backup CronJob은 restore 전 suspended | upload 성공을 복구 가능성으로 오인하지 않음 | 외부 restore와 RPO/RTO 기록 후 활성화 |
| D20 | BE CI 연결 전 `deployment.enabled: false` | 아직 존재하지 않는 GHCR 계약으로 Pod가 반복 실패하지 않게 함 | trusted publish + digest PR 흐름 검증 후 환경별 활성화 |
| D21 | `api.university.neordinary.com`, `api-dev.university.neordinary.com`, `api-pr-<PR>.university.neordinary.com`, `grafana.university.neordinary.com` | 모바일 직접 호출과 환경 식별이 명확한 exact host 계약 | API gateway 또는 별도 cluster 도입 시 재검토 |
| D22 | `university.neordinary.com` child zone을 Route 53으로 위임하고 exact record는 ExternalDNS가 관리 | DNS 권한과 workload record lifecycle 분리; Terraform은 사용하지 않음 | 상위 DNS 운영 주체나 도메인 변경 시 재검토 |
| D23 | Route 53 direct A record + cert-manager DNS-01; Preview는 `*.university.neordinary.com` Certificate와 `preview-wildcard-tls`를 공유 | 별도 proxy 없이 DNS·TLS 계층을 단순화하고 PR별 인증서 발급·갱신을 제거 | WAF·CDN·DDoS proxy가 실제로 필요해질 때 별도 edge 도입 |
| D24 | API와 Grafana는 direct HTTPS로 공개하고 Grafana는 local login + 기본 Viewer 계정을 사용 | 팀원은 VPN 없이 dashboard를 보고 익명 접근·회원가입은 차단 | 인원이 늘어 계정 회수가 어렵거나 조직 SSO·MFA가 필요할 때 재검토 |
| D25 | 8 vCPU, 32GiB RAM, 512GB NVMe에서 시작하고 Service quota를 DB 생성보다 먼저 예약해 preview를 최대 3개로 제한 | DB·관측 스택과 transient Job 여유를 포함한 단일 노드 운영 기준 | 관측치로 request 또는 동시 실행 수를 조정할 때 재산정 |
| D26 | Preview는 native Google/Kakao/Apple token login만 검증 | 모바일 앱은 provider token을 API에 직접 전달해 동적 callback이 불필요 | browser OAuth가 필요해질 때 callback 설계를 별도 검토 |
| D27 | 공인 SSH 개인 계정·공개키와 DB 터널 전용 권한, 외부 22·443만 허용 | 동적 팀원 IP와 VPN 구독 없이 접근·개인별 회수; root/비밀번호 SSH 금지 | 중앙 신원·MFA·기기 정책 또는 공개 SSH 노출 축소가 필요할 때 VPN/접근 proxy 재검토 |

## 신뢰 경계

`umc-product-infra/main` 쓰기 권한은 사실상 cluster-admin 권한이다. AppProject는 실수와 일부 supply-chain 범위를 줄이지만, trusted Git root 자체가 침해된 경우를 막지 못한다.

- built-in `default` AppProject는 source/destination을 허용하지 않는다.
- application, database, platform과 external-secrets namespace를 분리한다.
- ESO IAM role은 `/umc-product/{prod,dev,preview,...}` prefix별로 읽기를 제한한다.
- DB 관리자 Secret은 앱 namespace로 복제하지 않는다.
- preview label은 비용 gate이자 trusted same-repository PR에 대한 maintainer 승인이다.
- prod/dev/preview API는 모바일 앱이 직접 호출하는 Route 53 direct HTTPS 공개 endpoint다.
- Grafana는 production TLS와 실제 IDC IP를 검증한 뒤 public HTTPS로 열고 사람별 local account를
  `Viewer`로 발급한다. 인프라 관리자는 공인 SSH 개인 계정으로 health를 점검하고,
  SSH 자체가 실패할 때는 제공업체 console로 복구한다.
- SSH `admin`은 root에 준하는 sudo 권한이다. 백엔드 `db_tunnel` 계정은 지정 DB 목적지만
  forwarding할 수 있고 shell·sudo는 없다. 개인 DB role과 회수는 SSH 계정과 별도로 관리한다.
- cert-manager와 ExternalDNS는 서로 다른 Route 53 자격증명을 사용하고 exact hosted zone에만 변경 권한을 준다.
- Preview browser OAuth callback과 고정 callback broker는 운영하지 않는다.
- standard NetworkPolicy로 public HTTPS 목적지를 FQDN까지 제한할 수 없다는 잔여 위험을 수용한다.

## 데이터 경계

PostgreSQL 18 이미지의 기본 `PGDATA`는 `/var/lib/postgresql/18/docker`이고 PVC는 `/var/lib/postgresql`에 마운트한다. 이전 PG16 경로인 `/var/lib/postgresql/data`를 사용하지 않는다.

```text
prod    umc_product        owner umc_product_app
dev     umc_product_dev    owner umc_product_dev_app
preview umc_product_pr<N>  owner umc_product_preview_app
```

PostGIS와 `btree_gist`는 관리자 bootstrap이 먼저 설치한다. 앱 role은 Flyway와 runtime datasource에 함께 쓰이지만 superuser·`CREATEDB`·`CREATEROLE` 권한은 없다.

local-path PVC는 노드 소실을 견디지 못한다. prod DB의 복구선은 cluster 밖 S3 backup이며, writer는 기존 object의 List/Get/Delete 권한을 갖지 않는다. 복구에는 별도 read identity가 필요하다.

## 용량과 가용성

첫 IDC 사양은 **8 vCPU, 32GiB RAM, 512GB NVMe**를 권장 기준으로 확정한다. 4 vCPU는 PostgreSQL, 관측 스택, controller와 앱이 한 노드에서 겹칠 때 CPU burst 여유가 작다. 실제 구매·배포 전에는 전체 request 합, node allocatable, platform overhead와 디스크 지속 성능을 다시 확인한다. Preview는 동시에 최대 3개만 허용하고, 더 필요한 경우 먼저 실제 peak CPU/memory와 DB connection 수를 측정한다.

단일 노드에서 다음은 의도된 한계다.

- k3s control plane, app, DB, observability가 같은 장애 영역을 공유한다.
- local-path DB/telemetry PVC는 노드 장애로 손실될 수 있다.
- self-hosted monitoring만으로 node-offline을 알릴 수 없어 IDC 밖 uptime monitor가 필요하다.
- upgrade와 node maintenance에는 서비스 중단이 생긴다.

## Rollback 원칙

- Kubernetes live edit로 끝내지 않고 Git revert로 desired state를 복구한다.
- 앱은 known-good tag+digest PR로 되돌린다.
- DB write가 시작된 뒤에는 DNS만 과거 DB로 돌리는 rollback을 금지한다.
- secret rotation은 이전 AWS secret version → force-sync → role Job → 앱 restart 순으로 복구한다.
- backup 장애 시 `suspend: true`로 되돌리되 기존 object는 삭제하지 않는다.
- root prune 활성화 전에는 자식 Application 삭제를 별도 승인 작업으로 수행한다.

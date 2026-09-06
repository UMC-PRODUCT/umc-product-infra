# umc-infra

UMC Product의 단일 노드 K3s 클러스터를 선언적으로 운영하는 GitOps 저장소다.
서버 초기 구성부터 앱·DB·DNS/TLS·관측·AWS 외부 자원의 계약을 관리한다.

> [!IMPORTANT]
> 앱과 Ingress, 예약 backup은 기본적으로 안전장치가 닫혀 있다.
> 아래 `현재 안전장치`의 해제 조건을 검증한 뒤 필요한 gate만 하나씩 활성화한다.

## 인프라 다이어그램

처음 보는 경우 아래 순서대로 읽는다. 이미지를 누르면 원본 크기로 열리고, 생성 코드는
[다이어그램 디렉터리](docs/diagrams/README.md)에서 확인할 수 있다.

### 1. Traffic flow

사용자 요청, DNS/TLS, 앱·DB, backup과 관측 데이터의 전체 경로다.

[![UMC Product 트래픽 흐름](docs/diagrams/out/traffic-flow.png)](docs/diagrams/out/traffic-flow.png)

### 2. CI/CD flow

Backend CI가 GHCR에 이미지를 게시하고 GitOps PR을 거쳐 K3s에 배포하는 경로다.

[![UMC Product CI/CD 흐름](docs/diagrams/out/cicd-flow.png)](docs/diagrams/out/cicd-flow.png)

### 3. GitOps tree

Argo CD root Application부터 환경과 platform workload까지의 동기화 순서다.

[![UMC Product GitOps 구조](docs/diagrams/out/gitops-tree.png)](docs/diagrams/out/gitops-tree.png)

### 4. Secret supply chain

로컬 원장의 비밀값이 AWS Secrets Manager와 External Secrets를 거쳐 Pod에 도달하는 경로다.

[![UMC Product Secret 공급 경로](docs/diagrams/out/secret-supply-chain.png)](docs/diagrams/out/secret-supply-chain.png)

### 5. Branch flow

기능 브랜치가 dev와 prod로 승격되고 문제가 생기면 복구되는 경로다.

[![UMC Product 브랜치 흐름](docs/diagrams/out/branch-flow.png)](docs/diagrams/out/branch-flow.png)

### 6. Preview environment

승인된 PR의 preview 환경이 생성·검증·삭제되는 수명주기다.

[![UMC Product Preview 환경](docs/diagrams/out/preview-env.png)](docs/diagrams/out/preview-env.png)

## 작업 시작점

문서를 전부 순서대로 읽을 필요는 없다. 하려는 작업의 시작점 하나만 연다.

| 하려는 일 | 시작점 |
|---|---|
| 빈 서버에 처음 설치하거나 Ansible을 재실행 | [Ansible README](ansible/README.md) |
| Secret을 추가·변경·회전 | [비밀값 관리](docs/guides/secrets.md) |
| DNS·TLS·Route 53을 변경 | [도메인과 TLS](docs/guides/domains-tls.md) |
| backup을 처음 활성화 | [Backup activation runbook](runbooks/backup-activation.md) |
| 구조와 용어가 낯섦 | [저장소 처음 읽는 가이드](docs/guides/repository-tour.md) |
| 이 구조를 선택한 이유를 확인 | [K3s 아키텍처 결정 기록](docs/architecture/k3s.md) |

## 무엇을 관리하나

| 계층 | 도구 | 이 저장소의 위치 | 역할 |
|---|---|---|---|
| 서버 초기 구성 | Ansible | `ansible/` | Ubuntu·Tailscale·OpenSSH·UFW·K3s·Argo CD |
| GitOps | Argo CD | `bootstrap/`, `argocd/` | Git과 Kubernetes 상태 일치 |
| 애플리케이션 | Helm | `charts/umc-product-server/` | prod/dev/preview 공통 배포 계약 |
| 비밀값 공급 | External Secrets | `charts/umc-secrets/` | AWS source를 namespace별 Secret으로 동기화 |
| 앱 Secret 반영 | Reloader | `argocd/applications/platform/reloader.yaml` | 승인한 Secret 변경 시 단일 앱 Pod 재시작 |
| 데이터 | Kubernetes manifest | `manifests/postgres/` | PostgreSQL 18·PostGIS 3.6·backup |
| DNS/TLS | ExternalDNS·cert-manager | `argocd/`, `manifests/cert-manager/` | DNS record와 Let's Encrypt 인증서 |
| 관측 | Prometheus·Loki·Tempo·OTel·Grafana | `observability/`, `argocd/` | metrics·logs·traces·alert |
| AWS 외부 자원 | CloudFormation | `cloud/aws/` | IAM·Secrets Manager 접근·S3·SES·Route53 |

Terraform은 사용하지 않는다. CloudFormation은 AWS 외부 자원에만 사용하고, Kubernetes 자원은
Argo CD가 관리한다. Azure VM 또는 최종 IDC 생성, 상위 도메인의 NS 위임과 backend 코드는
이 저장소의 관리 범위가 아니다.

## 폴더 지도

```text
umc-infra/
├── ansible/                 # 빈 서버 → K3s·Argo CD
├── bootstrap/               # Argo CD root Application
├── argocd/                  # AppProject와 환경별 Application
├── charts/                  # 앱과 ExternalSecret Helm chart
├── manifests/               # cluster·DB·TLS·관측 raw manifest
├── cloud/aws/               # IAM·S3·SES·Route53 CloudFormation
├── observability/           # dashboard와 alert 원본
├── docs/                    # 아키텍처·작업 가이드
├── runbooks/                # backup 활성화 절차
└── scripts/                 # 생성·검증·일회성 bootstrap 도구
```

## 환경

환경별 host 계약은 다음과 같다.

| 환경 | source | namespace | API host |
|---|---|---|---|
| prod | `main` | `app`, `db` | `api.university.neordinary.com` |
| dev | `develop` | `dev-app`, `dev-db` | `api-dev.university.neordinary.com` |
| preview | trusted PR head SHA | `preview` | `api-pr-<PR>.university.neordinary.com` |

환경별 DB, resource, S3/SES와 확장 조건은 [아키텍처 결정 기록](docs/architecture/k3s.md),
DNS와 접근 정책은 [도메인/TLS 가이드](docs/guides/domains-tls.md)를 따른다.

## 현재 안전장치

| 영역 | 초기 상태 | 해제 조건 |
|---|---|---|
| 앱 | `deployment.enabled: false` | 검증된 GHCR tag·digest와 runtime 계약 |
| 외부 접근 | `ingress.enabled: false` | DNS, production Certificate와 edge 검증 |
| DNS | Hosted Zone 확정·공인 IP placeholder | 고정 Public IPv4를 모든 target과 `/32` filter에 동일 반영 |
| TLS | Let's Encrypt staging | DNS-01 성공 후 production issuer |
| DB backup | `suspend: true` | S3 upload와 외부 restore rehearsal |
| root 삭제 | `prune: false` | 첫 운영 안정화와 삭제 복구 절차 검증 |

이 값들은 오류가 아니라 준비되지 않은 배포를 막는 gate다. 각 행의 해제 조건을 검증하고
한 번에 하나씩 별도 변경으로 연다.

## 로컬 검증

저장소 루트에서 실행한다.

```bash
./scripts/validate.sh
```

Helm render, YAML과 Kubernetes schema, CloudFormation, Ansible, 관측 설정과 저장소 자체 계약을
검사한다. 로컬에 필요한 도구가 없으면 일부 단계가 skip될 수 있으므로 전체 출력을 읽고,
첫 bootstrap 전에는 GitHub Actions의 성공도 확인한다.

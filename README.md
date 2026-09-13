# UMC Product Infrastructure

UMC Product의 단일 노드 K3s 서버, 애플리케이션 배포, PostgreSQL, DNS/TLS와 모니터링을 관리하는 저장소다.

- **Ansible**은 서버 초기 구성과 SSH 접근을 관리한다.
- **Argo CD**는 Git에 반영된 Helm values와 manifest를 읽어 Kubernetes에 배포한다.
- **CloudFormation**은 IAM·S3·SES 등 AWS 외부 자원을 관리한다.

## 읽는 순서

계정과 비밀값은 승인된 팀 내 채널로 전달받는다. 도구 로그인, API 문서 비밀번호, 애플리케이션 Access Token,
SSH 계정과 DB 계정은 서로 별개다. DB 접속은 [DataGrip SSH 터널 안내](docs/guides/ansible-bootstrap.md#datagrip-접속)를 따른다.

장애 원인을 모르겠다면 Grafana의 `UMC PRODUCT System Overview`에서 시작한다.
[상황별 대시보드 안내](observability/README.md#어떤-상황에-어떤-대시보드를-볼까)에서 상세 화면을 고를 수 있다.

[인프라 다이어그램](#인프라-다이어그램) · [환경과 배포](#환경과-배포) ·
[수정 위치](#무엇을-어디서-수정하나) · [작업별 가이드](#작업별-가이드) · [변경과 검증](#변경과-검증)

## 인프라 다이어그램

요청 경로부터 배포·Secret·Preview까지 여섯 그림을 순서대로 볼 수 있다.
그림은 구조를 설명하며 실시간 배포 상태를 표시하지 않는다. 생성 원본과 읽는 방법은
[다이어그램 안내](docs/diagrams/README.md)에 정리되어 있다. 이미지를 누르면 원본 크기로 열린다.

### 1. Traffic flow

사용자 요청, DNS/TLS, 앱·DB, backup과 관측 데이터의 전체 경로다.

[![UMC Product 트래픽 흐름](docs/diagrams/out/traffic-flow.png)](docs/diagrams/out/traffic-flow.png)

### 2. CI/CD flow

Backend CI가 GHCR에 이미지를 게시하고, 선택한 환경 values를 사전 검증한 뒤
infra `main`에 직접 반영하여 K3s에 배포하는 경로다.

[![UMC Product CI/CD 흐름](docs/diagrams/out/cicd-flow.png)](docs/diagrams/out/cicd-flow.png)

### 3. GitOps tree

Argo CD root Application부터 환경과 platform workload까지의 동기화 순서다.
그림에 생략된 Reloader·Argo 접속 구성·모니터링 통합도 있으므로, 정확한 목록과 wave는
[`argocd/applications/`](argocd/applications/)와 [AppProject 선언](argocd/projects.yaml)을 기준으로 확인한다.

[![UMC Product GitOps 구조](docs/diagrams/out/gitops-tree.png)](docs/diagrams/out/gitops-tree.png)

### 4. Secret supply chain

AWS Secrets Manager → ESO → namespace별 Secret → workload 경로다. ESO 최초 접속 키는 Ansible로 별도 주입한다.
그림의 고정 개수 대신 [Secret 매핑 원본](charts/umc-secrets/values.yaml)에서 구성 목록을 확인한다.

[![UMC Product Secret 공급 경로](docs/diagrams/out/secret-supply-chain.png)](docs/diagrams/out/secret-supply-chain.png)

### 5. Branch flow

기능 브랜치가 dev와 prod로 승격되는 경로다. 운영 복구는 정상 동작하던 image tag·digest를 Git에 반영한다.
그림의 UI rollback을 영구 복구 절차로 사용하지 않으며, 이미지 rollback과 DB 복원은 별개 작업이다.

[![UMC Product 브랜치 흐름](docs/diagrams/out/branch-flow.png)](docs/diagrams/out/branch-flow.png)

### 6. Preview environment

승인된 내부 PR의 Preview 환경과 DB가 생성·삭제되는 수명주기다.
사용 전 [Preview 사용 조건](docs/guides/preview-environments.md#사용-조건)을 확인한다.
TLS는 그림의 PR별 Certificate와 달리 [공용 wildcard Certificate](manifests/cert-manager/preview-wildcard-certificate.yaml)를 사용하며,
PR 삭제 시 이 공용 인증서는 삭제하지 않는다.

[![UMC Product Preview 환경](docs/diagrams/out/preview-env.png)](docs/diagrams/out/preview-env.png)

## 환경과 배포

아래 브랜치는 **백엔드 저장소** 기준이다. Argo CD가 배포 설정을 읽는 브랜치는 **infra 저장소의 `main`**이다.

| 환경 | 용도 | 백엔드 소스 | 앱 / DB namespace |
|---|---|---|---|
| dev | 통합 개발·검증 | `develop` | `dev-app` / `dev-db` |
| prod | 운영 | `main` | `app` / `db` |
| preview | 승인된 PR 검증 | 내부 PR | `preview` |

### 애플리케이션 배포

1. 백엔드의 대상 브랜치에 변경을 병합한다.
2. 백엔드 CI가 검증한 이미지를 GHCR에 발행하고, 해당 환경의 image tag·digest를 infra `main`에 반영한다.
3. Argo CD가 변경된 values를 읽어 배포한다.
4. Argo CD의 동기화·Pod 상태와 해당 환경의 API 응답을 확인한다.

일상 배포마다 Ansible을 다시 실행하지 않는다. Preview는 위 브랜치 배포와 별도 흐름이므로
[사용 조건과 수명주기](docs/guides/preview-environments.md)를 먼저 확인한다.
이미지 발행·infra 반영 권한·rollback은 [GitHub 배포 가이드](docs/guides/github-trust-root.md)를 따른다.

## 무엇을 어디서 수정하나

| 변경하려는 것 | 원본 위치 |
|---|---|
| 환경별 앱 이미지·환경변수·CPU·메모리 | [앱 Helm chart](charts/umc-product-server/)의 `values-dev.yaml`, `values-prod.yaml`, `values-preview.yaml` |
| 서버 초기 구성·개인 SSH 계정·방화벽 | [ansible/](ansible/) |
| 배포할 Application·프로젝트 권한·동기화 순서 | [bootstrap/](bootstrap/), [argocd/](argocd/) |
| PostgreSQL·백업 Job | [manifests/postgres/](manifests/postgres/) |
| Secret 경로와 namespace별 매핑 | [charts/umc-secrets/](charts/umc-secrets/) — 실제 값은 Git에 넣지 않음 |
| DNS/TLS controller·인증서 | [argocd/applications/platform/](argocd/applications/platform/), [manifests/cert-manager/](manifests/cert-manager/) |
| 모니터링 스택 설정 | [관측 스택 배포 설정](argocd/applications/platform/observability/) |
| 대시보드·알림 규칙 | [observability/](observability/) — 원본 수정 후 생성물 갱신 |
| AWS IAM·S3·SES 자원 | [cloud/aws/](cloud/aws/) |
| 작업 절차·생성 및 검증 도구 | [docs/](docs/), [runbooks/](runbooks/), [scripts/](scripts/README.md) |

비밀값 변경은 로컬 `.env.*` 편집만으로 배포되지 않는다.
[Secret 운영 가이드](docs/guides/secrets.md)의 AWS Secrets Manager → ESO 동기화 → 소비자 반영 순서를 따른다.
서버 구매·호스팅 계약과 백엔드 애플리케이션 코드는 이 저장소에서 관리하지 않는다.

## 작업별 가이드

### 백엔드 개발자

- **DB 연결:** [DataGrip 설정](docs/guides/ansible-bootstrap.md#datagrip-접속). 개인 SSH 계정과 허용된 DB 접속 정보를 먼저 전달받는다.
- **PR 검증:** [Preview 사용 조건](docs/guides/preview-environments.md#사용-조건). PR 번호별 URL·DB와 삭제 시 주의점을 확인한다.
- **장애 조사:** [상황별 대시보드](observability/README.md#어떤-상황에-어떤-대시보드를-볼까). 전체 → 앱·DB·Pod → 로그·트레이스 순으로 좁힌다.
- **도구 계정·접속:** [Grafana](docs/guides/monitoring-access.md#팀원-계정), [Argo CD](docs/guides/ansible-bootstrap.md#argo-cd-공개-접속과-복구).

### 인프라 담당자

- **처음 구조를 읽을 때:** [저장소 가이드](docs/guides/repository-tour.md), [아키텍처와 설계 이유](docs/architecture/k3s.md).
- **서버 설치·SSH 계정 등록과 회수:** [Ansible 실행 안내](ansible/README.md), [개인 계정과 DB 터널](docs/guides/ansible-bootstrap.md#개인-계정과-db-터널).
- **Secret·DNS·TLS 변경:** [Secret 운영](docs/guides/secrets.md), [도메인과 TLS](docs/guides/domains-tls.md).
- **모니터링 수정:** [대시보드·알림 원본 관리](observability/README.md#대시보드와-알림-원본-관리), [스택 배포 구조](argocd/applications/platform/observability/README.md).
- **백업 준비·복원 검증:** [Backup runbook](runbooks/backup-activation.md).
- **서버 이전·DB 복원:** [Cafe24 이전 runbook](runbooks/cafe24-migration.md). DB 복원과 DNS 전환 순서를 임의로 바꾸지 않는다.

## 변경과 검증

일반 인프라 변경은 원본 수정 → 로컬 검증 → PR·CI → `main` 병합 → Argo CD 반영 확인 순서로 진행한다.
자동 이미지 갱신 계정의 권한은 [GitHub 배포 가이드](docs/guides/github-trust-root.md)에서 별도로 관리한다.

저장소 루트에서 실행한다.

```bash
./scripts/validate.sh
```

검사 범위와 필요한 도구는 [검증 도구 안내](scripts/README.md#5-저장소-전체-검증)에 있다.
로컬 검사에서 도구 부족으로 skip된 항목을 전체 성공으로 판단하지 말고,
[GitHub Actions](https://github.com/UMC-PRODUCT/umc-product-infra/actions)의 `Static validation`도 확인한다.

- Secret·개인키·DB 덤프·실제 사용자 정보는 커밋하지 않는다.
- 대시보드·알림은 `observability/` 원본을 수정하고 생성물을 함께 갱신한다.
- GitOps 관리 자원을 서버에서만 수정하고 끝내지 않는다.
- 삭제·복원·키 회전은 해당 가이드의 중단 조건과 복구 절차를 먼저 확인한다.

이 README는 작업의 시작점이다. 배포 진행률과 일회성 점검 결과는 Issue·PR 등 작업 기록에 남기고,
설정은 Git 원본에서, 실제 반영 여부는 Argo CD와 서비스에서 확인한다.

## 라이선스

별도 표시가 없는 이 저장소의 자체 작성 코드·설정·문서는
[GNU General Public License v3.0](LICENSE) (`GPL-3.0-only`)에 따라 사용·수정·재배포할 수 있다.
코드나 수정본을 배포할 때에는 GPL에 따라 저작권·라이선스 고지를 유지하고 해당 소스 코드를 제공해야 한다.
구체적인 조건은 [LICENSE](LICENSE) 원문을 따른다.

외부 프로젝트·라이브러리·아이콘 등 제3자 자료에는 각 권리자의 라이선스가 적용된다.
기존 저작권·라이선스 고지를 유지하며, 이 선언으로 제3자 자료를 재라이선스하지 않는다.
활용 시 원본 저장소 링크와 활용 사례를 공유해 주면 좋다. 이는 추가적인 라이선스 의무가 아닌 자발적 요청이다.

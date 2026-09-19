# UMC Product Infrastructure

[![Views](https://hits.sh/github.com/UMC-PRODUCT/umc-product-infra.svg?style=for-the-badge&label=views&color=4db6ac&labelColor=282828)](https://hits.sh/github.com/UMC-PRODUCT/umc-product-infra/)

UMC Product의 단일 노드 K3s 서버, 애플리케이션 배포, PostgreSQL, DNS/TLS와 모니터링을 관리하는 저장소다.

- **Ansible**은 서버 초기 구성과 SSH 접근을 관리한다.
- **Argo CD**는 Git에 반영된 Helm values와 manifest를 읽어 Kubernetes에 배포한다.
- **CloudFormation**은 IAM·S3·SES 등 AWS 외부 자원을 관리한다.

## 작업별 가이드

처음 읽는다면 [저장소 설명](docs/repository-tour.md)에서 도구와 코드의 역할을 살펴본다.

### 백엔드 개발자

- [DB 접속](docs/guides/db-access.md): 공개키 전달부터 DataGrip 연결까지.
- [Preview 사용](docs/guides/preview-environments.md): PR별 API·DB 사용과 종료 시 주의사항.
- [Grafana·Argo CD 사용](docs/guides/monitoring.md): 로그인, 대시보드 선택, 로그·배포 상태 조회.

### 인프라 담당자

- [계정·서버·배포 관리](docs/README.md#인프라-담당자): 하려는 작업에 맞는 관리자 절차를 찾는다.
- [백업·이전·복구](docs/README.md#위험-작업과-복구): 데이터나 서비스에 영향을 주는 작업의 사전 조건을 확인한다.

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

Argo CD root Application부터 환경과 platform workload까지의 구성과 sync wave를 보여준다.

[![UMC Product GitOps 구조](docs/diagrams/out/gitops-tree.png)](docs/diagrams/out/gitops-tree.png)

### 4. Secret supply chain

AWS Secrets Manager → ESO → namespace별 Secret → workload 경로다. ESO 최초 접속 키는 Ansible로 별도 주입한다.

[![UMC Product Secret 공급 경로](docs/diagrams/out/secret-supply-chain.png)](docs/diagrams/out/secret-supply-chain.png)

### 5. Branch flow

기능 브랜치가 dev와 prod로 승격되는 경로다. 운영 복구는 정상 동작하던 image tag·digest를 Git에 반영한다.
이미지 rollback과 DB 복원은 별개 작업이다.

[![UMC Product 브랜치 흐름](docs/diagrams/out/branch-flow.png)](docs/diagrams/out/branch-flow.png)

### 6. Preview environment

승인된 내부 PR의 Preview 환경과 DB가 생성·삭제되는 수명주기다.
사용 전 [Preview 사용 조건](docs/guides/preview-environments.md#사용-조건)을 확인한다.
TLS는 공용 wildcard Certificate를 사용하며, PR 삭제 시 이 공용 인증서는 삭제하지 않는다.

[![UMC Product Preview 환경](docs/diagrams/out/preview-env.png)](docs/diagrams/out/preview-env.png)

## 환경과 배포

아래 브랜치는 **백엔드 저장소** 기준이다. Argo CD가 배포 설정을 읽는 브랜치는 **infra 저장소의 `main`**이다.

| 환경 | 용도 | 백엔드 소스 | 앱 / DB namespace |
|---|---|---|---|
| dev | 통합 개발·검증 | `develop` | `dev-app` / `dev-db` |
| prod | 운영 | `main` | `app` / `db` |
| preview | 승인된 PR 검증 | 내부 PR | `preview` |

prod/dev는 백엔드 CI가 이미지를 발행하고 infra values의 tag·digest를 갱신하면 Argo CD가 배포한다.
일상 배포에 Ansible을 다시 실행하지 않는다. 실행·확인·rollback은
[배포 가이드](docs/operations/deployment.md), PR별 흐름은 [Preview 가이드](docs/guides/preview-environments.md)를 따른다.

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
| 사용자·운영·복구 절차 | [docs/](docs/) |
| 생성 및 검증 도구 | [scripts/](scripts/README.md) |

비밀값 변경은 로컬 `.env.*` 편집만으로 배포되지 않는다.
[Secret 운영 가이드](docs/operations/secrets.md)의 AWS Secrets Manager → ESO 동기화 → 소비자 반영 순서를 따른다.
서버 구매·호스팅 계약과 백엔드 애플리케이션 코드는 이 저장소에서 관리하지 않는다.

## 라이선스

별도 표시가 없는 이 저장소의 자체 작성 코드·설정·문서는
[GNU General Public License v3.0](LICENSE) (`GPL-3.0-only`)에 따라 사용·수정·재배포할 수 있다.
코드나 수정본을 배포할 때에는 GPL에 따라 저작권·라이선스 고지를 유지하고 해당 소스 코드를 제공해야 한다.
구체적인 조건은 [LICENSE](LICENSE) 원문을 따른다.

외부 프로젝트·라이브러리·아이콘 등 제3자 자료에는 각 권리자의 라이선스가 적용된다.
기존 저작권·라이선스 고지를 유지하며, 이 선언으로 제3자 자료를 재라이선스하지 않는다.
활용 시 원본 저장소 링크와 활용 사례를 공유해 주면 좋다. 이는 추가적인 라이선스 의무가 아닌 자발적 요청이다.

# UMC Product Infrastructure

[![Views](https://hits.sh/github.com/UMC-PRODUCT/umc-product-infra.svg?style=for-the-badge&label=views&color=4db6ac&labelColor=282828)](https://hits.sh/github.com/UMC-PRODUCT/umc-product-infra/)

UMC Product의 단일 노드 K3s 서버, 애플리케이션 배포, PostgreSQL, DNS/TLS와 모니터링을 관리하는 저장소다.

- **Ansible**은 서버 초기 구성과 SSH 접근을 관리한다.
- **Argo CD**는 Git에 반영된 Helm values와 manifest를 읽어 Kubernetes에 배포한다.
- **CloudFormation**은 IAM·S3·SES 등 AWS 외부 자원을 관리한다.

## 문서 안내

이 README는 **인프라 구성과 각 코드의 역할**을 설명한다.
팀원의 접속·계정 등록·배포·점검 방법은 [운영 문서 목차](docs/README.md)에서 찾는다.

- 구조가 처음이라면 [저장소 설명](docs/guides/repository-tour.md)과 [설계 이유](docs/architecture/k3s.md)를 읽는다.
- 인프라 담당자는 [팀 운영 가이드](docs/guides/infra-operations.md)에서 시작한다.
- 새 서버 설치는 [초기 구성 가이드](docs/guides/ansible-bootstrap.md)를 따른다. 운영 서버의 팀원 추가와는 별개다.

[인프라 다이어그램](#인프라-다이어그램) · [환경과 배포](#환경과-배포) · [코드 위치](#무엇을-어디서-수정하나)

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

## 변경과 운영 절차

인프라 설정은 원본 수정 → 로컬 검증 → PR·CI → `main` 병합 → Argo CD 반영 확인 순서로 관리한다.
SSH 계정과 AWS 자원은 Git에 올리는 것만으로 적용되지 않으므로 별도 실행·검증이 필요하다.

- [인프라 팀 운영 가이드](docs/guides/infra-operations.md): 계정 등록·회수, 작업별 적용 방식, 배포·점검.
- [문서 목차](docs/README.md): DB 접속, 모니터링, Secret·DNS·TLS와 복구 절차.
- [검증 도구 안내](scripts/README.md#5-저장소-전체-검증): `./scripts/validate.sh`의 검사 범위와 필요한 도구.

비밀값·개인키·실제 inventory·DB 덤프는 Git에 넣지 않는다.
README에는 구성 설명을 유지하고, 배포 진행률과 일회성 점검 결과는 Issue·PR에 기록한다.

## 라이선스

별도 표시가 없는 이 저장소의 자체 작성 코드·설정·문서는
[GNU General Public License v3.0](LICENSE) (`GPL-3.0-only`)에 따라 사용·수정·재배포할 수 있다.
코드나 수정본을 배포할 때에는 GPL에 따라 저작권·라이선스 고지를 유지하고 해당 소스 코드를 제공해야 한다.
구체적인 조건은 [LICENSE](LICENSE) 원문을 따른다.

외부 프로젝트·라이브러리·아이콘 등 제3자 자료에는 각 권리자의 라이선스가 적용된다.
기존 저작권·라이선스 고지를 유지하며, 이 선언으로 제3자 자료를 재라이선스하지 않는다.
활용 시 원본 저장소 링크와 활용 사례를 공유해 주면 좋다. 이는 추가적인 라이선스 의무가 아닌 자발적 요청이다.

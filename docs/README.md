# UMC 인프라 문서

[루트 README](../README.md)는 **무엇으로 구성되어 있는지**, 이 디렉터리는 **팀원이 어떻게 운영하는지**를 안내한다.
인프라 담당자는 [팀 운영 가이드](guides/infra-operations.md)에서 시작한다.

## 구조 이해

- [저장소 처음 읽기](guides/repository-tour.md): Ansible·Argo CD·Helm·AWS의 역할과 폴더별 원본.
- [아키텍처 결정 기록](architecture/k3s.md): 현재 구성을 선택한 이유와 재검토 조건.
- [인프라 다이어그램](../README.md#인프라-다이어그램): 트래픽·배포·Secret·Preview 흐름.

## 인프라 팀 운영

| 하려는 일 | 가이드 |
|---|---|
| 새 팀원 공개키 등록·권한 부여 | [SSH 계정 등록](guides/infra-operations.md#새-팀원-ssh-계정-등록) |
| 팀원 퇴임·접근 회수 | [SSH·DB 접근 회수](guides/infra-operations.md#팀원-회수) |
| 빈 서버 최초 구성 | [Ansible bootstrap](guides/ansible-bootstrap.md) |
| 이미지 배포·GitHub 인증·복구 | [GitHub 배포 가이드](guides/github-trust-root.md) |
| 환경변수·Secret 변경·키 회전 | [비밀값 관리](guides/secrets.md) |
| DNS·TLS·접속 오류 | [도메인과 TLS](guides/domains-tls.md) |
| Grafana 사용자·접근 관리 | [모니터링 접근](guides/monitoring-access.md) |
| Argo CD 접근·복구 | [Argo CD 운영](guides/ansible-bootstrap.md#argo-cd-공개-접속과-복구) |
| 대시보드·알림 수정 | [관측 원본 관리](../observability/README.md#대시보드와-알림-원본-관리) |
| 모니터링 스택 설정 | [관측 스택 구조](../argocd/applications/platform/observability/README.md) |
| 변경 검증 | [검증 도구](../scripts/README.md#5-저장소-전체-검증) |

## 백엔드 팀에 공유할 안내

- [DataGrip DB 접속](guides/infra-operations.md#datagrip-접속): 개인 SSH 계정과 환경별 DB 계정을 구분한다.
- [Preview 사용](guides/preview-environments.md): PR별 접속·DB와 삭제 시 주의점을 확인한다.
- [상황별 대시보드](../observability/README.md#어떤-상황에-어떤-대시보드를-볼까): 전체 현황 → 앱·DB·Pod → 로그·트레이스 순으로 조사한다.

실제 접속 주소·계정·비밀번호는 승인된 팀 채널로 별도 전달한다.
개인키·Passphrase를 받지 않으며, `.env.*`, 실제 inventory와 DB 덤프를 문서나 Git에 넣지 않는다.
도구 로그인, API 문서 비밀번호, 애플리케이션 Access Token, SSH 계정과 DB 계정은 서로 별개다.

## 삭제·이전·복구 작업

- [DB 자동 백업 활성화·복원 검증](../runbooks/backup-activation.md)
- [서버 이전·DB 복원·DNS 전환](../runbooks/cafe24-migration.md)
- [모니터링 수집 스택 교체](../runbooks/monitoring-operator-migration.md) — 일상 점검용이 아닌 교체 작업 절차.

이 작업들은 해당 runbook의 사전 조건·중단 조건·복구 절차를 먼저 읽는다.
문서의 선언값만으로 실제 배포 완료를 판단하지 않고, 실행 결과와 서비스 응답을 확인한다.

## 문서 유지보수

- 구성·설계 이유는 README와 `architecture/`, 실행 방법은 `guides/`, 위험 작업은 `runbooks/`에 둔다.
- 같은 실행 절차를 여러 README에 복사하지 않고 기준 가이드로 연결한다.
- 배포 진행률·일회성 점검 결과는 Issue·PR에 남긴다.
- 그림을 바꾸면 [다이어그램 유지보수](guides/diagram-maintenance.md)에 따라 원본과 생성물을 함께 갱신한다.

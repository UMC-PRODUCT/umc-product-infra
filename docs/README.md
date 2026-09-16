# 문서 목차

[루트 README](../README.md)는 인프라 구성과 코드의 역할을 소개한다.
여기서는 **사용하려는 팀원**과 **설정을 변경하는 관리자**를 나눠 필요한 절차로 안내한다.

## 백엔드 개발자

서버 관리자 권한 없이 따라 할 수 있는 안내다. 실제 주소·계정·비밀번호는 승인된 팀 채널로 전달받는다.

| 하려는 일 | 문서 |
|---|---|
| 공개키를 전달하고 dev·prod·Preview DB에 연결 | [DB 접속](guides/db-access.md) |
| PR의 API·DB로 기능 검증, 테스트 환경 종료 | [Preview 사용](guides/preview-environments.md) |
| Grafana 로그인, 대시보드·로그·트레이스 조회, Argo CD 배포 상태 확인 | [Grafana·Argo CD 사용](guides/monitoring.md) |

## 인프라 담당자

계정과 설정을 바꾸는 절차다. **운영 서버에 팀원만 추가할 때는 새 서버 설치를 실행하지 않는다.**

| 하려는 일 | 문서 |
|---|---|
| 팀원 SSH 공개키 등록·DB 터널 허용·접근 회수 | [SSH 계정 관리](operations/ssh-access.md) |
| 빈 서버를 K3s·Argo CD까지 최초 구성 | [서버 초기 구성](operations/ansible-bootstrap.md) |
| 앱·인프라 배포, GitHub 권한 설정, 이미지 rollback | [배포 관리](operations/deployment.md) |
| 환경변수·비밀번호·외부 서비스 키 변경 | [Secret 운영](operations/secrets.md) |
| DNS 레코드·TLS 인증서·공개 접속 경로 변경 | [DNS·TLS 운영](operations/domains-tls.md) |
| Grafana·Argo CD 계정·권한 관리, 로그인 경로 점검·복구 | [도구 접근 관리](operations/tool-access.md) |
| 대시보드·알림 규칙 수정 | [관측 원본 관리](../observability/README.md) |
| Prometheus·Loki·Tempo·Collector 구성 변경 | [관측 스택 배포 구조](../argocd/applications/platform/observability/README.md) |
| 수정 내용의 정적 검증 | [검증 스크립트](../scripts/README.md#저장소-전체-검증) |

## 위험 작업과 복구

데이터 삭제·서비스 중단 가능성이 있다. 각 문서의 대상, 사전 조건, 중단·복구 조건을 읽은 뒤 실행한다.

- [DB 자동 백업 활성화와 외부 복원 검증](runbooks/backup-activation.md)
- [서버 이전·DB 복원·DNS 전환](runbooks/cafe24-migration.md)
- [모니터링 수집 스택 교체](runbooks/monitoring-operator-migration.md)
- [Preview 활성화 준비·배포 실패·DB 정리 실패 대응](runbooks/preview-operations.md)

## 구조를 이해하고 싶다면

- [저장소 설명](repository-tour.md): 도구별 책임과 각 폴더의 역할.
- [아키텍처와 설계 이유](architecture/k3s.md): 구성의 전제와 재검토 기준.
- [인프라 다이어그램](../README.md#인프라-다이어그램): 요청·배포·Secret·Preview 흐름.
- 코드 옆 README: [Ansible](../ansible/README.md), [스크립트](../scripts/README.md), [관측 원본](../observability/README.md).

## 문서를 추가하거나 수정할 때

- `guides/`: 서비스를 이용하는 팀원의 작업. 관리자 명령은 넣지 않는다.
- `operations/`: 설정·접근 권한을 변경하는 관리자의 정상 운영 절차.
- `runbooks/`: 삭제·이전·복구처럼 위험 작업의 명령과 중단 조건.
- `architecture/`: 설계 이유. `diagrams/`: 그림 원본과 생성물.
- 한 절차는 한 문서에서 관리하고 다른 문서에서는 링크한다. 폴더마다 목차를 복제하지 않는다.
- 그림은 [유지보수 절차](operations/diagram-maintenance.md)에 따라 원본과 PNG를 함께 갱신한다.

비밀값·개인키·Passphrase·실제 inventory·DB 덤프는 Git에 넣지 않는다.
공개 문서에는 Grafana·Argo CD의 실제 접속 주소 대신 전달받을 항목과 확인 방법을 적는다.
배포 진행률과 일회성 점검 결과는 Issue·PR에 기록하며, 문서 수정만으로 적용 완료를 선언하지 않는다.

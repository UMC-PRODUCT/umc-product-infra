# Ansible 서버 구성

Ubuntu 서버의 개인 SSH 접근·방화벽·K3s·Argo CD 초기 구성을 관리한다.
서버 관리는 **공인 OpenSSH와 개인별 Linux 계정·공개키**를 사용하며,
DB는 SSH 터널로 접속한다. DB 5432와 Kubernetes API 6443은 외부에 공개하지 않는다.

실행 절차는 docs에서 관리한다.

- 운영 서버의 팀원 등록·회수: [인프라 팀 운영 가이드](../docs/operations/ssh-access.md).
- 빈 서버 최초 설치: [초기 구성 가이드](../docs/operations/ansible-bootstrap.md).
- 서버 이전·DB 복원·DNS 전환: [이전 runbook](../docs/runbooks/cafe24-migration.md).
- 전체 문서: [운영 문서 목차](../docs/README.md).

## 두 playbook의 역할

| 원본 | 역할 | 사용 시점 |
|---|---|---|
| [ssh-access.yml](playbooks/ssh-access.yml) | 개인 계정·공개키·SSH 권한·UFW 설정 | 팀원 등록·회수, 최초 관리자 접근 준비 |
| [bootstrap.yml](playbooks/bootstrap.yml) | SSH·OS → K3s → Argo CD → ESO 최초 자격증명 → root Application | 빈 서버 초기 구성·승인된 재구성 |

팀원 한 명을 추가하기 위해 전체 bootstrap을 실행하지 않는다.
앱 이미지·Helm values·Kubernetes manifest의 일상 배포는 Ansible이 아니라 Argo CD가 담당한다.

## 코드를 읽는 순서

팀원 등록을 이해하려면 `playbooks/ssh-access.yml`에서 시작해 `roles: ssh_access`가 호출하는
`roles/ssh_access/tasks/main.yml`을 읽는다. playbook은 **어떤 서버에 어떤 순서로 작업할지**,
role의 task 파일은 **계정·키·권한을 실제로 어떻게 설정할지**를 담는다.
`hosts.yml`은 이 작업에 넣는 접속 정보와 계정 목록이며, 파일을 저장하는 것만으로 서버가 바뀌지 않는다.

각 `name` 위의 한국어 주석은 작업 내용과 이유를 설명한다. 영어 작업명은 실행 로그에서
해당 위치를 찾는 이름이다. handler의 이름·`listen`은 `notify`의 연결 대상이기도 하므로
이름을 바꿀 때는 호출하는 쪽도 함께 확인한다.

| 코드 | 읽는 방법 |
|---|---|
| `roles`, `include_role` | 역할을 호출한다. 기본 시작 파일은 해당 역할의 `tasks/main.yml`이다. |
| `tasks_from`, `include_tasks`, `import_tasks` | 기본 파일 대신 지정한 작업 목록을 실행하거나 불러온다. |
| `loop`, `item` | 계정 같은 목록을 반복 처리한다. `item`은 현재 처리 중인 항목이다. |
| `when`, `assert` | `when`은 실행 조건, `assert`는 조건이 맞지 않으면 중단하는 검증이다. |
| `register`, `changed_when` | 실행 결과를 변수에 저장하고, 결과를 변경으로 보고할지 결정한다. |
| `notify`, `handlers`, `flush_handlers` | 변경 시 후속 작업을 예약한다. `flush_handlers`는 예약된 작업을 그 시점에 실행한다. |
| `delegate_to: localhost`, `become: true` | 전자는 관리자 PC에서 실행, 후자는 권한 상승(기본 sudo)을 뜻한다. |
| `templates/*.j2`, `defaults/main.yml` | 템플릿에 변수를 채워 설정 파일을 만들며, defaults는 덮어쓸 수 있는 기본값이다. |

## 설정 원본

기본 inventory는 `inventories/idc-new/hosts.yml`이다.
Git에는 [예제](inventories/idc-new/hosts.example.yml)만 보관하고 실제 서버·개인 계정 목록은 로컬에서 관리한다.
운영 inventory를 예제나 새 팀원 한 명의 설정으로 덮어쓰지 않는다.

| 위치 | 관리하는 것 |
|---|---|
| [roles/ssh_access/](roles/ssh_access/) | 개인 계정·키, 관리자와 DB 터널 사용자 권한 |
| [roles/common/](roles/common/) | OS 패키지·UFW·swap·커널 설정 |
| [roles/k3s/](roles/k3s/) | K3s·Traefik |
| [roles/argocd/](roles/argocd/) | Argo CD 초기 설치와 root Application |
| [roles/external_secrets_bootstrap/](roles/external_secrets_bootstrap/) | ESO의 AWS 최초 접근 자격증명 |

`admin`은 shell과 root 수준 sudo 권한을 갖는다. `db_tunnel`은 허용된 DB 목적지의
local TCP forwarding만 가능하며 shell·명령 실행·sudo는 차단된다. DB 내부 계정 권한은 별개다.

## 적용 전 주의

SSH·UFW 변경 전 기존 관리자 세션과 제공업체 console 복구 경로를 확보한다.
새 개인 관리자 SSH·sudo 검증 후 root 직접 로그인을 차단하며,
운영 중인 서버의 `ssh_access_finalize: true`는 팀원 추가 때도 유지한다.

UFW 인바운드 허용·제한 규칙은 선언값으로 수렴하므로 선언하지 않은 규칙은 삭제될 수 있다.
Ansible 실패가 자동 롤백을 뜻하지 않는다.
명령과 성공·거부 검증 기준은 [운영 가이드](../docs/operations/ssh-access.md)를 따른다.

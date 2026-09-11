# `common` 역할

개인별 공개키 SSH로 관리하는 x86_64 Ubuntu 22.04/24.04 서버를 K3s용으로 준비한다.
`ssh_access` 역할이 계정과 키 전용 SSH 인증을 먼저 구성한 뒤 실행한다.

## 하는 일

1. 지원 OS와 공인 포트 계약을 검증한다.
2. 필수 package와 시간대를 설정하고 swap을 끈다.
3. Kubernetes kernel module과 sysctl을 적용한다.
4. UFW 인바운드를 다음 상태로 수렴시킨다.
   - 인터넷에서 오는 `22/tcp`는 `limit`, `443/tcp`는 `allow`
   - K3s Pod·Service CIDR 허용
   - 공인 `80/tcp`, `5432/tcp`, `6443/tcp` 및 그 밖의 미선언 포트는 기본 차단
   - 전환 준비 단계에서만 이미 존재하는 `tailscale0` 규칙 보존

UFW `limit`은 같은 출발지에서 반복되는 새 SSH 연결을 제한한다. SSH 키 유출이나 대규모
DDoS 방어를 대신하지 않으며, 같은 NAT를 쓰는 팀원이 동시에 재접속하면 정상 연결도
일시 제한될 수 있다. DB 접근 대상과 사용자 권한은 별도의 `ssh_access` 역할에서 제한한다.

## 주요 변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `common_timezone` | `Asia/Seoul` | 서버 시간대 |
| `common_packages` | 공통 package 목록 | CA, curl, YAML, UFW 등 |
| `common_public_tcp_ports` | `[22, 443]` | 이 두 포트만 허용하는 고정 계약 |
| `ssh_access_finalize` | `false` | 공인 개인 관리자 접속 검증 후에만 기존 관리망 규칙 제거 |
| `ssh_access_public_verified` | 미설정 | `ssh_access`가 현재 공인 개인 관리자 접속을 검증한 결과 |

`k3s_cluster_cidr`, `k3s_service_cidr`은 각각 `10.42.0.0/16`, `10.43.0.0/16`이 기본값이다.
전체 bootstrap과 access-only playbook이 같은 값을 사용하며 실제 inventory가 우선한다.

## 파일 구성

| 경로 | 역할 |
|---|---|
| `defaults/main.yml` | 공통 package, 시간대, 공인 port 기본값 |
| `tasks/main.yml` | OS·Kubernetes host 설정과 방화벽 task 호출 |
| `tasks/firewall.yml` | access-only playbook과 bootstrap이 공유하는 UFW 설정 |
| `handlers/main.yml` | sysctl 적용 |
| `templates/k3s-modules.conf.j2` | 부팅 시 로드할 kernel module |
| `templates/k3s-sysctl.conf.j2` | Kubernetes network sysctl |

## 주의사항

- 이 role은 모든 UFW 인바운드 `allow`·`limit` 규칙을 소유하고 선언 밖의 규칙을 삭제한다.
- 먼저 공인 SSH `limit`을 추가하고 기존의 제한 없는 `allow 22/tcp`를 제거한다. 규칙 정리 후
  SSH `limit` 존재와 전체 선언 목록을 다시 검증한다.
- `ssh_access_finalize: false`이고 `tailscale0`가 실제 존재하면 전환용 관리 경로를 보존한다.
  새 서버에 Tailscale을 설치하거나 새 관리망 interface를 만들지는 않는다.
- 최종 단계는 `ssh_access_public_verified: true`를 요구한다. SSH·sudo 검증을 건너뛰기 위해
  이 값을 inventory에서 임의로 지정하지 않는다. Tailscale 제거는 access playbook이 담당한다.
- inbound deny/reject, route allow/limit, 안전하게 해석할 수 없는 profile rule은 자동 삭제하지 않고
  변경 전에 실패한다. 제공업체 console 등 독립된 복구 경로를 준비한다.
- SSH 설정·계정 관리는 `ssh_access` 역할이 담당하며 이 role은 sshd를 변경하지 않는다.

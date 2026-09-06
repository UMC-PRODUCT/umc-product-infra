# `common` 역할

Tailscale에 등록된 x86_64 Ubuntu 22.04/24.04 서버를 K3s용으로 준비한다.
표준 `bootstrap.yml`은 현재 SSH 세션이 Tailscale 경로인지 먼저 확인한 뒤 이 role을 실행한다.

## 하는 일

1. 지원 OS와 공인 포트 계약을 검증한다.
2. 필수 package와 시간대를 설정하고 swap을 끈다.
3. Kubernetes kernel module과 sysctl을 적용한다.
4. UFW 인바운드를 다음 상태로 수렴시킨다.
   - `tailscale0` interface 허용
   - 인터넷에서 오는 `443/tcp` 허용
   - K3s Pod·Service CIDR 허용
   - port 기반 `22/tcp`, `6443/tcp` 허용 규칙 없음
5. 기존 OpenSSH의 비밀번호 로그인을 끄고 공개키 인증만 허용한다.
6. SSH 설정을 검증하고 reload한 뒤 Tailscale 주소로 재연결한다.

`tailscale0` UFW 허용은 관리망 패킷을 호스트에 전달하기 위한 규칙이다. 실제 관리자는 tailnet
policy에서 `tag:umc-idc`의 `tcp:22`만 접근할 수 있어야 한다. Tailscale SSH를 사용하지 않는다.

## 주요 변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `common_timezone` | `Asia/Seoul` | 서버 시간대 |
| `common_packages` | 공통 package 목록 | CA, curl, YAML, UFW 등 |
| `common_public_tcp_ports` | `[443]` | 공인 port 계약. `22`, `6443` 금지 |
| `tailscale_interface` | `tailscale0` | 관리망 interface |

`k3s_cluster_cidr`, `k3s_service_cidr`도 전체 inventory 변수에서 제공된다.

## 파일 구성

| 경로 | 역할 |
|---|---|
| `defaults/main.yml` | 공통 package, 시간대, 공인 port 기본값 |
| `tasks/main.yml` | OS, UFW, OpenSSH 설정 |
| `handlers/main.yml` | sysctl 적용과 SSH 검증·reload |
| `templates/k3s-modules.conf.j2` | 부팅 시 로드할 kernel module |
| `templates/k3s-sysctl.conf.j2` | Kubernetes network sysctl |
| `templates/ssh-hardening.conf.j2` | 기존 OpenSSH 보안 설정 |

## 주의사항

- 이 role은 모든 UFW 인바운드 `allow`·`limit` 규칙을 소유하고 선언 밖의 규칙을 삭제한다.
- 필요한 `tailscale0` 규칙을 먼저 추가한 뒤 기존 공인 SSH 규칙을 지워 잠금 위험을 줄인다.
- inbound deny/reject, route allow/limit, 안전하게 해석할 수 없는 profile rule은 자동 삭제하지 않고
  변경 전에 실패한다.
- 최초 Tailscale 등록은 이 role이 아니라 `playbooks/tailscale-enroll.yml`에서 수행하며, 그 단계는
  UFW와 sshd를 바꾸지 않는다.

# `tailscale` 역할

IDC 서버에 고정 버전 Tailscale을 설치하고 `tag:umc-idc` node identity를 검증한다.
관리 SSH 자체는 기존 OpenSSH와 공개키/PEM을 사용하며 Tailscale SSH는 항상 끈다.

## 두 가지 실행 방식

| 호출 위치 | 동작 |
|---|---|
| `playbooks/tailscale-enroll.yml` | 미등록 node의 최초 등록을 허용 |
| `playbooks/bootstrap.yml` | 이미 등록된 node 상태만 검증하며 암묵적으로 재등록하지 않음 |

최초 등록 playbook은 공인 SSH를 임시로 사용하지만 UFW와 sshd를 변경하지 않는다. 등록이 끝나면
inventory의 `ansible_host`를 출력된 `100.x` 주소 또는 MagicDNS 이름으로 바꾸고 Ansible ping을
통과시킨 뒤 표준 bootstrap을 실행한다.

## 최초 등록 동작

1. x86_64 Ubuntu 22.04/24.04와 고정 hostname·tag 계약을 확인한다.
2. 공식 stable APT key와 repository를 설치하고 Tailscale package 버전을 pin한다.
3. `tailscaled`를 활성화한다.
4. 이미 online이면 재등록하지 않는다.
5. 미등록이면 controller의 `TAILSCALE_AUTH_KEY` 또는 private prompt에서 auth key를 받는다.
6. key를 원격 `/run`의 임시 `0600` 파일로 전달하고 `tailscale up`에 file 방식으로 넘긴다.
7. 성공·실패와 관계없이 임시 key 파일을 삭제한다.
8. online, hostname, 단일 `tag:umc-idc`, `100.64.0.0/10` 주소, 설치 버전을 검증한다.
9. Tailscale SSH·web UI·자동 update가 꺼졌고 route를 수락·광고하지 않으며 netfilter가
   `on`인지 실제 client preference를 읽어 확인한다.

적용되는 핵심 network 설정은 다음과 같다.

```text
--advertise-tags=tag:umc-idc
--accept-dns=true
--accept-routes=false
--advertise-routes=
--ssh=false
--netfilter-mode=on
```

APT pin을 우회하는 client 자체 update와 불필요한 web UI도 `tailscale set`으로 끈다.

## 주요 변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `tailscale_version` | role에 고정 | 설치할 package 버전 |
| `tailscale_hostname` | `inventory_hostname` | tailnet node 이름 |
| `tailscale_required_tag` | `tag:umc-idc` | 허용되는 유일한 node tag |
| `tailscale_interface` | `tailscale0` | `common` role이 허용할 interface |
| `tailscale_up_timeout_seconds` | `120` | 최초 등록 제한 시간 |
| `tailscale_enrollment_enabled` | `false` | 표준 bootstrap의 암묵적 등록 방지 |

`tailscale_enroll.yml`만 `tailscale_enrollment_enabled: true`로 덮어쓴다. tag와 interface 계약은
임의로 넓히지 않는다.

## Auth key 규칙

Tailscale 관리자 console에서 **one-off, non-ephemeral, `tag:umc-idc`용 key**를 만든다.
기본 private prompt 사용을 권장한다. 실제 값은 inventory, Git, `--extra-vars`, 명령행 인자에
넣지 않는다. 자동화가 필요하면 승인된 비밀 저장소가 `TAILSCALE_AUTH_KEY` 환경변수를 process에
주입하게 한다.

이 key는 서버를 tailnet에 등록하는 최초 열쇠다. ESO의 AWS access key와는 관계없다.

## 외부 전제

이 role은 tailnet policy를 변경하지 않는다.
[`../../tailscale-policy.example.hujson`](../../tailscale-policy.example.hujson)을 실제 policy에
병합해 관리자 그룹에서 `tag:umc-idc`의 `tcp:22`만 허용해야 한다. 더 넓은 default grant가 있으면
그 grant가 제한을 무력화할 수 있으므로 함께 제거한다.

등록 후 UFW 수렴과 공인 SSH 규칙 제거는 `common` role의 책임이다. 전체 bootstrap과 검증이
끝난 다음 제공업체 방화벽/NSG의 공인 `22/tcp` 허용도 사람이 제거한다.

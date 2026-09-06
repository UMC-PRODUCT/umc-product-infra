# Ansible 서버 초기 구성

이 디렉터리는 빈 Ubuntu 서버 한 대를 UMC Product용 K3s 클러스터로 구성한다.
관리 접속은 **Tailscale 경유 기존 OpenSSH + 공개키/PEM**을 사용한다. Tailscale SSH는
활성화하지 않으며, Kubernetes API `6443/tcp`도 외부에 열지 않는다.

처음 설치할 때는 반드시 다음 두 단계를 분리한다.

```text
임시 공인 SSH
  └─ tailscale-enroll.yml: Tailscale 설치·등록만 수행
           ↓ inventory 주소를 Tailscale로 변경하고 ping 확인
Tailscale 경유 OpenSSH
  └─ bootstrap.yml: common → k3s → argocd → secret-zero → root Application
```

> [!CAUTION]
> `bootstrap.yml`은 대상 서버의 모든 UFW 인바운드 `allow`·`limit` 규칙을 소유한다.
> 선언하지 않은 규칙은 삭제할 수 있고 SSH 비밀번호 인증과 swap도 끈다. 제공업체 console 같은
> SSH 외부 복구 경로와 Tailscale 접속을 확인하기 전에는 실행하지 않는다.

## 준비할 것

- 관리자 PC: Python 3.12 이상, OpenSSH, Tailscale 로그인, 대상 서버용 private key
- 대상 서버: Ubuntu 22.04/24.04 x86_64, Python 3, 공개키 SSH와 sudo
- 복구 경로: SSH와 독립된 제공업체 console
- Tailnet policy: 관리자 그룹에서 `tag:umc-idc`의 `tcp:22`만 허용
- Tailscale auth key: `tag:umc-idc`용 one-off, non-ephemeral key
- AWS: Secrets Manager source와 ESO bootstrap access key

Tailnet policy는 Ansible이 계정에 반영하지 않는다.
[`tailscale-policy.example.hujson`](tailscale-policy.example.hujson)을 실제 policy에 병합하고,
`tag:umc-idc`에 도달하는 더 넓은 기본 grant가 있다면 제거한다.

## 최초 설치

저장소 루트에서 도구와 inventory를 준비한다.

```bash
cd ansible
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
ansible-galaxy collection install -r collections/requirements.yml
cp -n inventories/idc/hosts.example.yml inventories/idc/hosts.yml
```

### 1. 공인 SSH로 Tailscale 등록

`hosts.yml`을 다음 상태로 채운다.

- `ansible_host`: 대상 서버의 임시 공인 IP/DNS
- `ansible_user`: 공개키/PEM과 sudo를 사용할 계정
- `tailscale_hostname`: tailnet에서 사용할 서버 이름
- `tailscale_initial_admin_cidrs`: 지금 서버에 접속하는 관리자 공인 IP의 `/32`
- `tailscale_enroll_confirm: true`
- `bootstrap_confirm: false`

현재 공인 SSH와 sudo가 되는지 확인한 뒤 등록 playbook을 실행한다.

```bash
ansible k3s_servers -m ansible.builtin.ping
ansible k3s_servers --become -m ansible.builtin.ping
ansible-playbook playbooks/tailscale-enroll.yml
```

기본 실행은 auth key를 화면에 보이지 않는 prompt로 묻는다. 승인된 비밀 저장소가
`TAILSCALE_AUTH_KEY`를 Ansible process에 주입하는 자동화도 지원한다. 실제 key를 inventory,
Git 파일, `--extra-vars`, 명령행 인자에 넣지 않는다.

이 playbook은 다음만 수행한다.

- 공식 APT 저장소에서 고정 버전 Tailscale 설치
- `tag:umc-idc`, `--ssh=false`, `--netfilter-mode=on`으로 등록
- 임시 `0600` key 파일을 사용한 뒤 항상 삭제
- 관리자 PC에서 등록된 `100.x` 주소의 기존 OpenSSH `22/tcp` 도달 확인

UFW와 sshd는 이 단계에서 변경하지 않는다.

### 2. Tailscale 주소로 전환

등록 출력에 나온 `100.x` 주소 또는 MagicDNS 이름으로 직접 로그인한다. 같은 서버의 host key인지
제공업체 console에서 fingerprint를 대조한다.

```bash
ssh USER@TAILSCALE_ADDRESS
```

성공하면 `hosts.yml`을 다음처럼 바꾼다.

- `ansible_host`: Tailscale `100.x` 주소 또는 MagicDNS 이름
- `tailscale_enroll_confirm: false`
- `bootstrap_confirm: true`

Ansible도 반드시 Tailscale 경로로 연결되는지 확인한다.

```bash
ansible k3s_servers -m ansible.builtin.ping
ansible k3s_servers --become -m ansible.builtin.ping
```

### 3. 표준 bootstrap 실행

```bash
ansible-playbook --syntax-check playbooks/bootstrap.yml
ansible-lint playbooks/bootstrap.yml playbooks/tailscale-enroll.yml
ansible-playbook --check --tags preflight playbooks/bootstrap.yml
ansible-playbook playbooks/bootstrap.yml
```

별도 key 파일은 `--private-key /absolute/path/to/private-key`, sudo 비밀번호는
`--ask-become-pass`를 붙인다.

`bootstrap.yml`은 현재 SSH peer와 서버 쪽 목적지 주소를 Tailscale daemon으로 검증한 뒤에만
UFW와 SSH를 바꾼다. `common` role은 `tailscale0` 인바운드를 허용하고 공인 `22/tcp`와
`6443/tcp` 허용 규칙을 남기지 않는다. 실제 관리자 범위는 tailnet grant가 제한한다.

전체 bootstrap 성공 후 제공업체 방화벽/NSG의 임시 공인 `22/tcp` 허용도 제거한다.

## 성공 확인

대상 서버에 Tailscale로 SSH 접속해 확인한다.

```bash
sudo k3s kubectl get nodes
sudo k3s kubectl get applications -n argocd
sudo k3s kubectl get secretstores,externalsecrets -A
sudo k3s kubectl get certificates,clusterissuers -A
sudo ufw status verbose
```

완료 기준은 다음과 같다.

- `PLAY RECAP`의 `failed=0`, `unreachable=0`
- K3s Node가 `Ready`
- Argo CD root Application이 `Synced`, `Healthy`
- 필요한 SecretStore, ExternalSecret, Certificate, ClusterIssuer가 준비됨
- UFW에 source와 무관하게 `22/tcp`, `6443/tcp` 허용 규칙이 없음
- 제공업체 방화벽에도 공인 `22/tcp` 허용이 없음

`6443/tcp`를 노트북에 열거나 kubeconfig를 복사하지 않는다. 클러스터 조회는 Tailscale 경유
OpenSSH 세션에서
`sudo k3s kubectl ...`로 수행한다.

## 재실행과 변경 경계

Ansible 설정 변경이나 중간 실패 복구는 inventory가 계속 Tailscale 주소를 가리키는 상태에서
`playbooks/bootstrap.yml` 전체를 재실행한다. 이미 등록된 서버에
`tailscale-enroll.yml`을 반복 실행할 필요는 없다.

| 변경 | 방법 |
|---|---|
| Ansible bootstrap 설정 변경·중간 실패 | 검사 후 `bootstrap.yml` 재실행 |
| Helm values·manifest·앱 image 변경 | Git 반영 후 Argo CD 동기화 |
| AWS Secrets Manager 앱 값 변경 | ESO 동기화 사용 |
| ESO bootstrap access key 회전 | 새 key로 `bootstrap.yml` 실행 후 검증하고 이전 key 폐기 |

상세한 준비·오류 처리는 [Ansible bootstrap 상세 가이드](../docs/guides/ansible-bootstrap.md),
AWS key 준비는 [비밀값 관리](../docs/guides/secrets.md)를 따른다.

## 파일을 읽는 순서

1. [`playbooks/tailscale-enroll.yml`](playbooks/tailscale-enroll.yml): 최초 관리망 등록
2. [`playbooks/bootstrap.yml`](playbooks/bootstrap.yml): 전체 설치 순서와 Tailscale 연결 검증
3. [`inventories/idc/hosts.example.yml`](inventories/idc/hosts.example.yml): 두 단계 inventory 입력
4. [`roles/tailscale/`](roles/tailscale/): Tailscale package와 node identity
5. [`roles/common/`](roles/common/): OS·OpenSSH·UFW
6. [`roles/k3s/`](roles/k3s/): K3s·Traefik
7. [`roles/argocd/`](roles/argocd/): Helm으로 Argo CD 설치
8. [`roles/external_secrets_bootstrap/`](roles/external_secrets_bootstrap/): ESO secret-zero

각 role은 `defaults/main.yml`에서 입력값을 보고 `tasks/main.yml`에서 실행 순서를 읽는다.

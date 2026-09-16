# Ansible로 새 서버 초기 구성

빈 Ubuntu 서버 한 대에 개인 SSH 접근과 K3s·Argo CD, ESO 최초 접근 자격증명을 준비하는 절차다.
이미 운영 중인 서버의 팀원 등록·회수는 [SSH 접근 운영](ssh-access.md)을 따른다.
기존 서비스와 데이터를 옮기는 작업은 [Cafe24 이전 runbook](../runbooks/cafe24-migration.md)의
별도 inventory·DB 복원·DNS 전환 순서를 적용한다.

```text
기존 관리자 SSH 세션 + Cafe24 console 확보
  → 개인 계정·공개키 준비 (finalize=false)
  → 별도 공인 SSH 연결에서 개인 관리자·sudo 검증
  → 개인 관리자로 root SSH 차단 (finalize=true)
  → K3s·Argo CD 설치 + ESO 최초 접근 자격증명 준비
  → 선행 조건 확인 후 root Application 생성·GitOps 수렴 검증
```

> [!CAUTION]
> 새 개인 관리자 접속을 확인할 때까지 기존 관리자 SSH 세션과 Cafe24 console을 유지한다.
> SSH playbook과 bootstrap은 UFW 인바운드 `allow`·`limit` 규칙을 관리하므로 선언하지 않은
> 허용 규칙을 제거할 수 있다. Ansible 실패는 앞선 변경을 자동으로 롤백하지 않는다.

## 사전 준비

- 관리자 PC: Python 3.12 이상, OpenSSH, Passphrase로 보호한 개인 private key
- 새 서버: Ubuntu 22.04/24.04 x86_64, Python 3, 현재 관리자 키 인증 SSH와 sudo
- 복구: SSH와 독립된 Cafe24 console, 대조할 서버 SSH host key fingerprint
- 네트워크: 새 서버 고정 공인 IPv4, 제공업체 TCP 22·443 허용, outbound DNS·HTTPS
- bootstrap: 적용할 Git revision과 CI 확인, AWS Secrets Manager source·ESO key·Route 53 IAM 준비

제공업체 방화벽은 이 playbook이 변경하지 않는다. 서버 UFW에서 22번을 열어도 상위 방화벽이
막으면 접속되지 않는다. DB `5432/tcp`와 Kubernetes API `6443/tcp`는 외부에 열지 않는다.
private key는 관리자 PC에만 두고 공개키만 서버에 등록한다.

## 로컬 도구와 새 서버 inventory 준비

저장소 루트에서 로컬 도구를 설치한다.

```bash
cd ansible
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
ansible-galaxy collection install -r collections/requirements.yml
```

현재 운영 기본 inventory는 Git에서 제외된 `ansible/inventories/idc-new/hosts.yml`이다.
**이 파일을 새 서버 준비용으로 덮어쓰거나 운영 서버의 `ssh_access_finalize`를 `false`로 되돌리지 않는다.**
새 서버는 저장소 밖의 별도 파일을 사용한다. 아래 경로는 승인된 실제 저장 위치로 바꾼다.
기존 파일이 있으면 복사로 덮어쓰지 말고 대상 서버부터 다시 확인한다.

```bash
NEW_SERVER_INVENTORY='/absolute/path/outside-git/new-server-hosts.yml'
cp -n inventories/idc-new/hosts.example.yml "$NEW_SERVER_INVENTORY"
```

이후 명령은 관리자 PC의 `ansible/`에서 실행하며 대상 inventory를 항상 `-i`로 지정한다.
예제에 든 서버 주소는 새 서버 공인 IP로 교체하고 현재 운영 서버와 IP·host key가 다른지 대조한다.
`ssh_access_users`에는 승인된 개인 관리자와 실제 공개키를 넣는다. 개인키 경로는
`ansible_ssh_private_key_file`로 지정하거나 `--private-key /absolute/path/to/private-key`를 붙인다.
private key 본문·DB 비밀번호·AWS key를 inventory에 넣지 않는다.

| 입력 | 개인 계정 준비 | 최종 확정 |
|---|---|---|
| `ansible_host`, `ansible_user` | 새 서버의 현재 접속 가능 주소·관리자 | 새 공인 IP·선언한 개인 `admin` |
| `ssh_access_public_host` | 새 서버 공인 IPv4 | 동일 |
| `ssh_access_users` | 실제 개인 계정·역할·상태·공개키 전체 목록 | 기존 목록 보존 |
| `ssh_access_confirm` | 대상·계정·복구 경로 확인 후 `true` | `true` |
| `ssh_access_finalize` | `false` | 독립 SSH·sudo 검증 뒤 `true` |
| `common_public_tcp_ports` | `[22, 443]` | 동일 |
| `bootstrap_confirm` | `false` | 전체 설치 직전에 `true` |
| `bootstrap_root_app_enabled` | 예제 기본값 `false` | root 생성 선행 조건 확인 뒤 `true` |

서버 host key는 Cafe24 console에서 확인한 fingerprint와 대조해 관리자 PC의 `known_hosts`에
등록한다. host key 검증을 끄지 않는다. 다른 관리자 PC로 옮기면 fingerprint부터 다시 대조한다.

## 1. 개인 관리자 계정 준비

새 서버 inventory에 `ssh_access_finalize: false`를 둔다. 이 단계는 개인 계정·키와 SSH 정책을
준비하면서 현재 관리자의 키 접속 경로를 유지한다. 비밀번호 로그인을 계속 허용하는 단계는 아니다.
대상이 새 서버 한 대인지 확인하고 연결·sudo·문법 검사를 거쳐 적용한다.

```bash
ansible -i "${NEW_SERVER_INVENTORY:?새 서버 inventory 절대 경로 필요}" k3s_servers --list-hosts
ansible -i "${NEW_SERVER_INVENTORY:?}" k3s_servers -m ansible.builtin.ping
ansible -i "${NEW_SERVER_INVENTORY:?}" k3s_servers --become -m ansible.builtin.ping
ansible-playbook -i "${NEW_SERVER_INVENTORY:?}" --syntax-check playbooks/ssh-access.yml
ansible-lint playbooks/ssh-access.yml
ansible-playbook -i "${NEW_SERVER_INVENTORY:?}" playbooks/ssh-access.yml
```

`admin`은 shell과 비밀번호 없는 sudo를 갖는 root 수준 권한이다. 실제 운영 담당자에게만 부여한다.
`ssh_access_users`는 전체 `AllowUsers` 목록이며 `public_keys`도 계정별 authorized_keys 전체를
교체한다. 승인된 관리자나 키를 빠뜨리지 않는다. DB가 없는 빈 서버는 먼저 관리자 계정만
준비하고 DB 배포 후 실제 Service IP를 조회해 터널 사용자를 추가할 수 있다.

## 2. 별도 연결 검증 후 SSH 확정

기존 관리자 세션은 유지한 채 별도 terminal에서 새 공인 IP에 개인 관리자로 접속한다.
아래 사용자명·공인 IP·키 경로를 본인 값으로 바꾼다.

```bash
ssh -o IdentitiesOnly=yes -i /absolute/path/to/private-key ADMIN_USER@NEW_PUBLIC_IP 'sudo -n true'
```

fingerprint와 대상 서버를 대조하고 새 로그인·sudo 성공을 확인한다. 실패하면 다음 단계로
넘어가지 않는다. DB가 이미 준비됐다면 [DB 접근 가이드](../guides/db-access.md)에서
승인된 DB 연결 성공과 shell·미승인 forwarding 거부도 확인한다.

검증 후 새 서버 inventory의 `ansible_host`를 `ssh_access_public_host`와 같은 공인 IP,
`ansible_user`를 선언한 개인 `admin`, `ssh_access_finalize`를 `true`로 바꾼다.
Ansible도 해당 개인 관리자의 키를 사용하도록 경로를 확인한다.

```bash
ansible -i "${NEW_SERVER_INVENTORY:?}" k3s_servers -m ansible.builtin.ping
ansible -i "${NEW_SERVER_INVENTORY:?}" k3s_servers --become -m ansible.builtin.ping
ansible-playbook -i "${NEW_SERVER_INVENTORY:?}" playbooks/ssh-access.yml
```

이제 root 직접 SSH 로그인이 차단된다. 별도 개인 관리자 SSH 연결과 `sudo -n true`를 다시
확인한 뒤 이전 관리자 세션을 종료한다. 실패하면 유지 중인 세션 또는 Cafe24 console에서
SSH 설정·UFW를 복구한다. DB/PVC·K3s 초기화나 host key 검증 해제로 우회하지 않는다.

## 3. K3s·Argo CD 설치와 ESO 자격증명 준비

새 서버 inventory의 `bootstrap_confirm: true`, `ssh_access_confirm: true`,
`ssh_access_finalize: true`와 검증된 개인 관리자 공인 SSH 경로를 사용한다.
예제의 `bootstrap_root_app_enabled: false`는 **첫 root Application 생성만 보류**한다.
기존 Application/ApplicationSet이 있으면 중단하며, 이미 운영 중인 앱을 정지시키는 옵션이 아니다.

실행 전 저장소 루트에서 `./scripts/validate.sh`를 수행하고 실패·건너뛴 검사를 확인한다.
다음 명령은 다시 관리자 PC의 `ansible/`에서 실행한다.

```bash
ansible-playbook -i "${NEW_SERVER_INVENTORY:?}" --syntax-check playbooks/bootstrap.yml
ansible-lint playbooks/bootstrap.yml playbooks/ssh-access.yml
ansible-playbook -i "${NEW_SERVER_INVENTORY:?}" --check --tags preflight playbooks/bootstrap.yml
ansible-playbook -i "${NEW_SERVER_INVENTORY:?}" playbooks/bootstrap.yml
```

빈 서버의 전체 `--check`가 설치 전후 상태를 완전히 모사하지는 않는다.
bootstrap은 `ssh_access` → `common` → `k3s` → `argocd` → `external_secrets_bootstrap`
순서로 구성하고, 활성화된 경우에만 마지막에 root Application을 만든다.
`external_secrets_bootstrap`은 namespace와 `aws-bootstrap` Secret만 준비한다.
ESO controller 자체는 root 활성화 뒤 Argo CD가 배포한다.

ESO key는 비표시 prompt 또는 승인된 비밀 저장소에서 제공한 `ESO_AWS_ACCESS_KEY_ID`,
`ESO_AWS_SECRET_ACCESS_KEY` 환경변수로 전달한다. 재실행에도 현재 key가 필요하다.
inventory·Git·명령행 인자·로그에 값을 넣지 않는다. 준비 방법은 [Secret 운영](secrets.md)을 따른다.

core 설치 후 새 서버에서 Node `Ready`, Argo CD 준비, Application/ApplicationSet 목록이 비어 있는지
확인한다. GitOps를 시작할 준비가 되면 Git revision, AWS source, DNS·TLS와 DB 준비 순서를
확인하고 `bootstrap_root_app_enabled: true`로 바꿔 같은 bootstrap 명령을 재실행한다.
이전 작업은 정상 root를 바로 열지 말고 [이전 runbook](../runbooks/cafe24-migration.md)의
Prepare → Verify → DNS 전환 순서와 root 경로 override를 유지한다.

## 4. 완료 검증

새 개인 관리자 SSH 세션에서 상태를 조회한다. kubeconfig는 노트북에 복사하지 않는다.

```bash
sudo -n k3s kubectl get nodes
sudo -n k3s kubectl get applications,applicationsets -A
sudo -n ufw status verbose
sudo -n sshd -t
```

root 활성화 후 ESO와 cert-manager가 설치되면 다음도 확인한다. core만 설치한 단계에서는
해당 CRD가 아직 없어 조회가 실패할 수 있다.

```bash
sudo -n k3s kubectl get secretstores,externalsecrets -A
sudo -n k3s kubectl get certificates,clusterissuers -A
```

- `PLAY RECAP`: `failed=0`, `unreachable=0`, K3s Node `Ready`
- root 활성화 후 필요한 Argo CD Application `Synced/Healthy`, SecretStore·ExternalSecret·Certificate 준비
- 공인 SSH `22/tcp`·HTTPS `443/tcp` 접근, 외부 DB `5432/tcp`·Kubernetes API `6443/tcp` 비공개
- 개인 관리자 새 SSH·sudo 성공, root 직접 접속·비밀번호 로그인 거부
- DB 배포 후 승인된 DB 터널 연결 성공과 shell·sudo·미승인 forwarding 거부

core만 설치했거나 DB가 아직 없으면 앱·DB 검증까지 끝났다고 표시하지 않는다.
일상 운영에서는 공인 IP·개인 관리자 inventory와 `ssh_access_finalize: true`를 유지한다.

## 개인 계정과 DB 터널

운영 서버의 권한 구분·공개키 준비·전체 허용 계정 보존·SSH 적용은
[SSH 접근 운영](ssh-access.md)을 따른다.
접속 계정만 바꾸려면 `ssh-access.yml`을 사용하며 전체 bootstrap을 재실행하지 않는다.

### DataGrip 접속

관리자의 DB Service IP 조회와 터널 허용 목적지 준비는 [SSH 접근 운영](ssh-access.md)을 따른다.
환경별 Database 이름, SSH/SSL과 General의 입력값은 [DB 접근 가이드](../guides/db-access.md)에 있다.
SSH 개인키와 PostgreSQL 계정·비밀번호는 서로 다른 자격증명이다.

### 팀원 회수

`state: absent` 적용, 기존 SSH 터널 종료 검증과 사용한 DB role에 따른 별도 회수·회전 검토는
[팀원 회수 절차](ssh-access.md#팀원-회수)를 따른다. 목록에서 항목만 지워 회수 완료로 간주하지 않는다.

## 운영과 문제 해결

Grafana·Argo CD 계정 발급·회수, 공개 접속 검증과 장애 시 SSH 복구는
[도구 접근 운영](tool-access.md)을 따른다.

### 재실행과 오류 확인

공인 IP·개인 관리자 inventory와 `ssh_access_finalize: true`를 유지한다.
SSH 사용자 변경은 `ssh-access.yml`, OS/K3s bootstrap 변경은 `bootstrap.yml`을 사용한다.
앱 image, Helm values와 Kubernetes manifest 변경은 Git과 Argo CD로 처리한다.

| 증상 | 먼저 확인할 것 |
|---|---|
| SSH timeout | 공인 IP, 카페24 상위 방화벽, UFW, sshd 상태 |
| `Permission denied (publickey)` | 사용자명, 본인 private key, 선언한 공개키, effective sshd 설정 |
| 관리자 sudo 실패 | `role: admin`, sudoers 검증, 개인 관리자 접속 여부 |
| finalize 거부 | 개인 관리자 공인 경로·키·sudo 검증, inventory 입력 |
| SSH host key 경고 | 제공업체 console의 fingerprint와 실제 서버 교체 여부 |
| DB 터널만 실패 | `permit_open`의 IP/포트, Service ClusterIP, Endpoint/Pod, 환경별 DB 계정 |
| UFW 규칙 때문에 중단 | deny/reject, route rule, quoted profile을 console에서 검토 |
| ESO 자격증명 누락 | [비밀값 관리](secrets.md)의 bootstrap key 준비 상태 |
| root Application health timeout | child Application, ExternalSecret, Certificate 상태 |

### 방화벽과 보호 경계

| 경로 | UFW | 인증/권한 |
|---|---|---|
| 공인 SSH | `22/tcp` 연결 제한 | 개인 키만, root 로그인 금지, 계정별 shell/터널 권한 |
| 공인 HTTPS | `443/tcp` 허용 | Traefik과 각 애플리케이션 인증 |
| K3s 내부 | Pod·Service CIDR 허용 | 단일 node 내부 통신과 NetworkPolicy |
| 공인 DB/API | `5432/tcp`, `6443/tcp` 허용 없음 | 제공업체 상위 방화벽도 비공개 |

UFW 기본 정책은 inbound deny, outbound allow다. `common`은 필요한 규칙을 먼저 추가한 뒤
선언 밖 인바운드 `allow`·`limit` 규칙을 제거한다. 자동 해석하기 위험한 inbound deny/reject,
route allow/limit, quoted application profile은 변경 전에 중단한다.

공개키 인증은 개인 키 유출·OpenSSH 취약점·접속 폭주를 모두 해결하지 않는다. 보안 업데이트와
SSH 인증 로그를 점검하고 계정 회수를 검증한다. 연결 제한이나 서버 UFW는 회선에 도달한 공격
트래픽을 없애지 않으므로 대규모 DDoS·초과 과금 대응은 제공업체 정책과 별도로 검토한다.

Route 53은 DNS만 제공하며 proxy나 방화벽이 아니다. public `80/tcp`는 DNS-01 인증에 필요하지
않아 열지 않는다. K3s는 Secret 저장 암호화와 `root:root`, mode `0600` kubeconfig를 유지한다.
클러스터 조회는 개인 관리자 SSH의 `sudo -n k3s kubectl`로 수행하고 kubeconfig를 배포하지 않는다.

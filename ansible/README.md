# Ansible 서버 초기 구성

이 디렉터리는 Ubuntu 서버 한 대를 UMC Product용 K3s 클러스터로 구성한다.
관리 접속은 **공인 SSH + 개인별 Linux 계정·공개키**를 사용한다. VPN 가입은 필요하지 않으며
DB `5432/tcp`와 Kubernetes API `6443/tcp`는 외부에 열지 않는다.

접속 경로를 바꾸는 작업과 전체 bootstrap은 분리한다.

```text
기존 관리자 SSH + 제공업체 console 확보
  → ssh-access.yml, finalize=false: 개인 계정·키와 공인 SSH 준비
  → 개인 관리자 공인 SSH·sudo, DB 터널 성공/권한 거부 확인
  → inventory를 공인 IP·개인 관리자 계정으로 변경
  → ssh-access.yml, finalize=true: root SSH 차단·기존 Tailscale 제거
  → bootstrap.yml: common → k3s → argocd → secret-zero → root Application
```

> [!CAUTION]
> 기존 관리자 세션과 제공업체 console을 확보한 채 새 연결을 검증한다. 공인 SSH가 막혀 있으면
> Tailscale을 먼저 제거하지 않는다. `bootstrap.yml`은 UFW 인바운드 `allow`·`limit` 규칙을
> 소유하므로 선언하지 않은 규칙을 삭제할 수 있다. Ansible 실패는 자동으로 롤백되지 않는다.

## 준비할 것

- 관리자 PC: Python 3.12 이상, OpenSSH, 개인 private key
- 대상 서버: Ubuntu 22.04/24.04 x86_64, Python 3, 기존 관리자 SSH와 sudo
- 복구 경로: SSH와 독립된 제공업체 console 및 대조할 SSH host key fingerprint
- 제공업체 방화벽: 공인 TCP 22·443 허용, DB 5432·Kubernetes API 6443 비공개
- AWS: Secrets Manager source와 ESO bootstrap access key

private key는 본인의 컴퓨터에만 둔다. 서버에는 `.pub` 공개키만 등록하며 실제 팀원 목록은
Git에서 제외된 inventory로 관리한다. 공인 SSH는 자동 스캔에 노출되므로 보안 업데이트와
접속 로그 점검을 유지한다. 연결 제한은 대규모 DDoS나 회선 과금을 막는 수단이 아니다.

## 최초 설치 또는 기존 Tailscale 서버 전환

저장소 루트에서 도구와 inventory를 준비한다.

```bash
cd ansible
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
ansible-galaxy collection install -r collections/requirements.yml
cp -n inventories/idc/hosts.example.yml inventories/idc/hosts.yml
```

기존 `hosts.yml`은 덮어쓰지 않는다. 별도 key 파일은 Ansible에
`--private-key /absolute/path/to/private-key`를 붙이거나 로컬 inventory에 경로만 지정한다.

### 1. 기존 연결을 유지하며 개인 계정 준비

`hosts.yml`에 아래 입력을 검토한다.

| 변수 | 1단계 값 |
|---|---|
| `ansible_host`, `ansible_user` | 현재 접속 가능한 기존 관리자 경로·계정 |
| `ssh_access_public_host` | 서버 고정 공인 IP, Cafe24는 `1.255.226.166` |
| `ssh_access_users` | 개인별 이름, 역할, 상태, 공개키와 허용 DB 목적지 |
| `ssh_access_confirm` | 대상·개인 계정·복구 경로를 검토한 뒤 `true` |
| `ssh_access_finalize` | `false` |
| `bootstrap_confirm` | 아직 `false` |

`ssh_access_users`의 `role`은 `admin` 또는 `db_tunnel`, `state`는
`present` 또는 `absent`다. `public_keys`에는 공개키 문자열만 넣는다.
`db_tunnel`의 `permit_open`에는 실제 DB Service ClusterIP와 `:5432`만 지정한다.
사용자 추가·회수와 DataGrip 입력은 [상세 가이드](../docs/guides/ansible-bootstrap.md#개인-계정과-db-터널)를 따른다.

```bash
ansible k3s_servers -m ansible.builtin.ping
ansible k3s_servers --become -m ansible.builtin.ping
ansible-playbook --syntax-check playbooks/ssh-access.yml
ansible-lint playbooks/ssh-access.yml
ansible-playbook playbooks/ssh-access.yml
```

이 단계에서는 기존 root/관리자 접속과 Tailscale을 유지한다. 제공업체 방화벽에서도 공인 22번을
허용해야 하며, 서버 UFW만 변경해 상위 방화벽을 우회할 수는 없다.

### 2. 공인 개인 계정 검증 후 전환 마무리

기존 세션을 닫지 말고 별도 terminal에서 직접 연결한다. 아래 이름과 key 경로는 본인 값으로 바꾼다.

```bash
ssh -o IdentitiesOnly=yes -i /absolute/path/to/private-key ADMIN_USER@1.255.226.166 'sudo -n true'
```

호스트 fingerprint가 같은 서버인지 확인한다. `db_tunnel` 사용자는 DataGrip의 SSH tunnel로
승인된 DB에 연결되며, shell·명령 실행과 미승인 목적지 연결은 거부되는지 확인한다.
DB가 아직 없는 빈 서버에서는 DB가 준비된 뒤 이 검증을 마치기 전 팀원에게 접속 완료로 안내하지 않는다.

검증 후 inventory를 다음처럼 바꾼다.

- `ansible_host`: `ssh_access_public_host`와 같은 공인 IP
- `ansible_user`: 선언한 개인 `admin` 계정
- `ssh_access_finalize: true`

```bash
ansible k3s_servers -m ansible.builtin.ping
ansible k3s_servers --become -m ansible.builtin.ping
ansible-playbook playbooks/ssh-access.yml
```

마무리는 root 직접 SSH를 차단하고 Tailscale service/package를 제거한다. 복구용 identity 파일은
보존한다. 새로운 개인 관리자 연결을 다시 열어 확인한 뒤에만 이전 세션을 종료한다.
전환 실패 시 기존 세션 또는 제공업체 console에서 복구하고, DB·K3s를 초기화하지 않는다.

### 3. 표준 bootstrap 실행

`bootstrap_confirm: true`, `ssh_access_finalize: true`와 개인 관리자 공인 SSH inventory를 사용한다.

```bash
ansible-playbook --syntax-check playbooks/bootstrap.yml
ansible-lint playbooks/bootstrap.yml playbooks/ssh-access.yml
ansible-playbook --check --tags preflight playbooks/bootstrap.yml
ansible-playbook playbooks/bootstrap.yml
```

ESO access key는 private prompt 또는 승인된 비밀 저장소의 환경변수로 전달한다.
inventory·Git·명령행 인자에 비밀값을 넣지 않는다. 이미 운영 중인 서버에서 SSH 계정만 변경할 때는
전체 bootstrap 대신 `ssh-access.yml`만 실행한다.

## 성공 확인

개인 관리자로 공인 SSH에 새로 접속해 확인한다.

```bash
sudo -n k3s kubectl get nodes
sudo -n k3s kubectl get applications -n argocd
sudo -n k3s kubectl get secretstores,externalsecrets -A
sudo -n k3s kubectl get certificates,clusterissuers -A
sudo -n ufw status verbose
sudo -n sshd -t
```

- `PLAY RECAP`의 `failed=0`, `unreachable=0`
- K3s Node가 `Ready`, 필요한 Argo CD Application이 `Synced`, `Healthy`
- 필요한 SecretStore, ExternalSecret, Certificate, ClusterIssuer가 준비됨
- 공인 SSH 22·HTTPS 443은 필요한 경로로 접근 가능, 5432·6443은 외부에서 접근 불가
- 비밀번호/root SSH 로그인 거부, 개인 관리자 재접속·sudo 성공
- DB 터널 사용자의 승인된 DB 연결 성공, shell·sudo·미승인 forwarding 거부

kubeconfig를 노트북에 복사하지 않는다. 클러스터 조회는 관리자 SSH 세션에서
`sudo -n k3s kubectl ...`로 수행한다. 일반 백엔드 사용자는 이 권한을 받지 않는다.

## 재실행과 변경 경계

| 변경 | 방법 |
|---|---|
| 개인 SSH 계정·공개키·터널 목적지·퇴임 처리 | inventory 갱신 후 `ssh-access.yml` 실행·회수 검증 |
| Ansible bootstrap 설정 변경·중간 실패 | 개인 관리자 공인 SSH로 검사 후 `bootstrap.yml` 재실행 |
| Helm values·manifest·앱 image 변경 | Git 반영 후 Argo CD 동기화 |
| AWS Secrets Manager 앱 값 변경 | ESO 동기화 사용 |
| ESO bootstrap access key 회전 | 새 key로 `bootstrap.yml` 실행 후 검증하고 이전 key 폐기 |

`state: absent`는 SSH 로그인 차단·세션 종료·계정 제거를 수행하며 home은 보존한다.
개인 DB role의 로그인 차단·DB 세션 종료는 별도 작업이다. inventory에서 항목만 지워
퇴임 처리했다고 간주하지 않는다.

상세 준비·오류 처리는 [Ansible bootstrap 상세 가이드](../docs/guides/ansible-bootstrap.md),
AWS key 준비는 [비밀값 관리](../docs/guides/secrets.md)를 따른다.
서버 이전은 [Cafe24 이전 절차](../runbooks/cafe24-migration.md)의 별도 inventory·DB·DNS 전환 조건을 따른다.

## 파일을 읽는 순서

1. [`playbooks/ssh-access.yml`](playbooks/ssh-access.yml): 개인 SSH 접근 준비·검증 후 전환
2. [`playbooks/bootstrap.yml`](playbooks/bootstrap.yml): 전체 설치 순서와 공인 SSH 검증
3. [`inventories/idc/hosts.example.yml`](inventories/idc/hosts.example.yml): 로컬 inventory 입력
4. [`roles/ssh_access/`](roles/ssh_access/): 개인 계정·키·SSH 권한·기존 VPN 정리
5. [`roles/common/`](roles/common/): OS·UFW·swap·kernel
6. [`roles/k3s/`](roles/k3s/): K3s·Traefik
7. [`roles/argocd/`](roles/argocd/): Helm으로 Argo CD 설치
8. [`roles/external_secrets_bootstrap/`](roles/external_secrets_bootstrap/): ESO secret-zero

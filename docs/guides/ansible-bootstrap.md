# Ansible로 단일 노드 K3s 초기 구성

빈 Ubuntu 서버는 곧바로 `bootstrap.yml`을 실행하지 않는다. 먼저 공인 SSH로 Tailscale에
등록하고, 관리 경로를 Tailscale로 바꾼 뒤 표준 bootstrap을 실행한다.

```text
1. tailnet policy와 one-off auth key 준비
2. 공인 SSH로 tailscale-enroll.yml 실행
3. inventory를 Tailscale 주소로 변경
4. OpenSSH + PEM 접속과 Ansible ping 확인
5. bootstrap.yml 실행
6. 결과 확인 후 제공업체의 공인 22/tcp 제거
```

> [!CAUTION]
> `bootstrap.yml`은 대상 서버의 모든 UFW 인바운드 `allow`·`limit` 규칙을 소유한다.
> 선언하지 않은 규칙을 삭제하고 SSH 비밀번호 인증과 swap을 끈다. 제공업체 console 같은
> SSH 외부 복구 경로와 Tailscale 접속이 모두 확인되지 않았다면 실행하지 않는다.

## 1. 구조와 용어

| 용어 | 뜻 |
|---|---|
| 관리자 PC | Ansible과 Tailscale client를 실행하는 컴퓨터 |
| 대상 서버 | 구성할 Ubuntu 서버 한 대 |
| inventory | 서버 주소와 설치 입력을 적는 로컬 `hosts.yml` |
| Tailscale auth key | 서버를 tailnet에 처음 등록할 일회용 key |
| secret-zero | ESO가 AWS Secrets Manager에 접근할 런타임 access key 쌍 |

Tailscale은 관리망만 제공한다. SSH 인증은 서버의 기존 OpenSSH와 공개키/PEM이 계속 담당한다.
role은 `tailscale up --ssh=false`를 사용하므로 Tailscale SSH를 켜지 않는다.

Kubernetes API `6443/tcp`는 공인망과 tailnet 모두에서 관리자 PC에 열지 않는다. 클러스터 명령은
대상 서버에 SSH 접속한 뒤 `sudo k3s kubectl ...`로 실행한다.

## 2. 사전 준비

### 관리자 PC와 대상 서버

- 관리자 PC: Python 3.12 이상, OpenSSH, Tailscale client 로그인, 대상 서버 private key
- 대상 서버: Ubuntu 22.04/24.04 x86_64, Python 3, 공개키 SSH와 sudo
- 대상 서버 outbound: DNS와 HTTPS 가능
- 복구 경로: SSH와 독립된 제공업체 console
- 운영 기준 자원: 8 vCPU, 32GiB RAM, 512GB 영속 disk

### Tailnet policy

[`tailscale-policy.example.hujson`](../../ansible/tailscale-policy.example.hujson)의 항목을 실제
tailnet policy에 병합한다. 예시의 관리자 email을 실제 값으로 바꾸고 다음 계약을 지킨다.

- 관리자 그룹만 `tag:umc-idc`의 `tcp:22`에 접근
- `tag:umc-idc`에 접근할 수 있는 더 넓은 기본 grant 제거
- `tag:umc-idc` 소유자도 관리자 그룹으로 제한

Ansible은 Tailscale 계정의 policy를 적용하지 않는다. 관리자 console에서 직접 반영하고 저장 결과를
확인한다. 관리자 PC 자체도 해당 tailnet에 로그인되어 있어야 한다.

### Tailscale auth key

대상 서버 등록용 key는 다음 속성으로 한 개 만든다.

- tag: `tag:umc-idc`
- one-off: 재사용하지 않음
- non-ephemeral: 일시적인 CI node가 아니라 지속 서버로 유지

tailnet에서 device approval을 사용한다면 key를 preauthorized로 만들거나 등록 직후 node를 승인해야
playbook의 online 검사를 통과할 수 있다.

기본적으로 private prompt에서 붙여 넣는다. 자동화 환경은 승인된 비밀 저장소에서
`TAILSCALE_AUTH_KEY` 환경변수로 Ansible process에 주입할 수 있다. key 값을 inventory,
Git 파일, `--extra-vars`, 명령행 인자에 넣지 않는다.

### 외부 시스템

표준 bootstrap 전에 다음도 준비한다.

- Git `main`과 성공한 CI
- AWS Secrets Manager source와 ESO bootstrap access key
- Route53 public hosted zone, authoritative NS 위임, Hosted Zone ID
- cert-manager와 ExternalDNS의 서로 다른 최소 권한 Route53 자격증명
- 제공업체 상위 방화벽/NSG 설정을 변경할 권한

AWS 준비 방법은 [비밀값 관리](secrets.md)를 따른다.

## 3. 관리자 PC 준비와 inventory 생성

저장소 루트에서 실행한다.

```bash
cd ansible
python3 --version
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
ansible-galaxy collection install -r collections/requirements.yml
cp -n inventories/idc/hosts.example.yml inventories/idc/hosts.yml
```

`-n`은 기존 `hosts.yml`을 덮어쓰지 않는다. 이 파일은 Git에 넣지 않는다.

최초 등록 전 inventory 값은 다음과 같다.

| 변수 | 최초 등록 값 |
|---|---|
| `ansible_host` | 임시 공인 SSH IP 또는 DNS |
| `ansible_user` | 공개키/PEM과 sudo를 사용할 계정 |
| `tailscale_hostname` | 서버의 고정 tailnet 이름, 기본 예시는 `umc-idc-01` |
| `tailscale_initial_admin_cidrs` | 현재 관리자 공인 출발지의 canonical `/32` |
| `tailscale_enroll_confirm` | 모든 등록 입력을 검토한 뒤 `true` |
| `bootstrap_confirm` | 아직 `false` |

`tailscale_initial_admin_cidrs`는 **등록 순간의 공인 SSH 안전 검사에만** 사용한다. 동적 IP가 바뀌면
새 주소의 `/32`로 갱신하고 등록을 실행한다. `/0`, host bit가 포함된 CIDR, `CHANGE_ME`는
preflight가 거부한다.

별도 private key 파일을 쓸 때는 아래 명령의 `ansible-playbook`과 `ansible`에
`--private-key /absolute/path/to/private-key`를 붙인다. sudo 비밀번호가 필요하면
`--ask-become-pass`를 붙인다.

## 4. 1단계: 공인 SSH로 Tailscale 등록

### 4.1 SSH host key와 연결 확인

제공업체 console에서 본 SSH host key fingerprint와 최초 접속 화면을 대조한다.
서버 key 변경 경고가 나오면 기존 기록을 먼저 지우지 말고 서버 교체 여부를 확인한다.

```bash
ssh USER@PUBLIC_ADDRESS
sudo -v
exit

ansible-inventory --graph
ansible k3s_servers -m ansible.builtin.ping
ansible k3s_servers --become -m ansible.builtin.ping
```

### 4.2 등록 playbook 실행

```bash
ansible-playbook --syntax-check playbooks/tailscale-enroll.yml
ansible-lint playbooks/tailscale-enroll.yml
ansible-playbook --check --tags preflight playbooks/tailscale-enroll.yml
ansible-playbook playbooks/tailscale-enroll.yml
```

`tailscale-enroll.yml`은 현재 SSH peer가 `tailscale_initial_admin_cidrs` 안에 있는지 확인한 뒤 다음만
수행한다.

1. 공식 APT 저장소에서 checksum과 버전을 고정한 Tailscale 설치
2. `tag:umc-idc`, 고정 hostname, `--ssh=false`, `--netfilter-mode=on`으로 등록
3. auth key를 원격 임시 `0600` 파일로 전달하고 성공·실패와 관계없이 삭제
4. node가 online이고 정확한 tag 하나와 `100.64.0.0/10` 주소를 가졌는지 확인
5. 관리자 PC에서 그 주소의 기존 OpenSSH `22/tcp`에 도달하는지 확인

이 playbook은 UFW와 sshd를 변경하지 않는다. 실패해도 공인 SSH 경로가 그대로 남으므로 원인을
수정하고 다시 실행한다. one-off key가 소비됐지만 node가 등록되지 않았다면 새 key를 발급한다.

## 5. 2단계: Tailscale 주소로 전환

등록 결과의 `100.x` 주소 또는 MagicDNS 이름으로 기존 PEM 로그인을 확인한다.

```bash
ssh USER@TAILSCALE_ADDRESS
```

새 주소에 대한 SSH host key가 표시되면 제공업체 console에서 같은 서버의 fingerprint인지 다시
대조한다. Tailscale SSH 인증 화면이 나타나는 구조가 아니라 기존 OpenSSH 공개키 로그인이어야 한다.

성공하면 `hosts.yml`을 다음 상태로 변경한다.

| 변수 | bootstrap 값 |
|---|---|
| `ansible_host` | Tailscale `100.x` 주소 또는 MagicDNS 이름 |
| `tailscale_enroll_confirm` | `false` |
| `bootstrap_confirm` | `true` |

이후 연결 검사는 공인 주소가 아니라 변경된 inventory로 실행한다.

```bash
ansible k3s_servers -m ansible.builtin.ping
ansible k3s_servers --become -m ansible.builtin.ping
```

`ping`이 실패하면 `bootstrap.yml`을 실행하지 않는다. 관리자 PC의 Tailscale 로그인, tailnet grant,
MagicDNS/IP, OpenSSH key와 기존 UFW를 확인한다.

## 6. 표준 bootstrap

### 6.1 변경 전 검사

```bash
ansible-playbook --syntax-check playbooks/bootstrap.yml
ansible-lint playbooks/bootstrap.yml playbooks/tailscale-enroll.yml
ansible-playbook --check --tags preflight playbooks/bootstrap.yml

cd ..
./scripts/validate.sh
cd ansible
```

`--tags preflight`는 inventory와 K3s CIDR 계약을 로컬에서 검사한다. 전체 `--check`는 빈 서버의
설치 전후 상태를 정확히 모사하지 못하므로 사용하지 않는다. 저장소 검사에서 도구가 없어 skip된
항목은 출력에서 확인한다.

### 6.2 ESO AWS 자격증명

`external_secrets_bootstrap`은 ESO가 AWS Secrets Manager를 읽을 access key ID와 secret access
key를 private prompt로 받는다. Tailscale auth key와 별개이며, bootstrap 재실행 때도 현재 값을
다시 입력한다. 자동화 환경은 승인된 비밀 저장소에서 다음 환경변수를 주입할 수 있다.

- `ESO_AWS_ACCESS_KEY_ID`
- `ESO_AWS_SECRET_ACCESS_KEY`

자격증명을 inventory, `--extra-vars`, Git 파일에 넣지 않는다.

### 6.3 실행

```bash
ansible-playbook playbooks/bootstrap.yml
```

실제 순서는 다음과 같다.

| 순서 | 구성 요소 | 작업 |
|---|---|---|
| 1 | preflight | SSH peer와 서버 목적지가 실제 Tailscale identity인지 확인 |
| 2 | `tailscale` | 고정 package·hostname·tag·online 상태 재검증 |
| 3 | `common` | OS, kernel, UFW, 기존 OpenSSH hardening |
| 4 | `k3s` | K3s와 Secret 저장 암호화 설치·검증 |
| 5 | `argocd` | 고정 Helm chart로 Argo CD 설치·검증 |
| 6 | `external_secrets_bootstrap` | ESO AWS secret-zero 생성·갱신 |
| 7 | root Application | GitOps tree 동기화와 health 대기 |

preflight는 `SSH_CONNECTION`의 peer를 `tailscale whois`로 조회하고, 연결 목적지 주소가 서버의
Tailscale 주소인지 확인한다. 공인 SSH로 잘못 실행하면 UFW 변경 전에 중단한다.

`common`은 `tailscale0` 인바운드를 먼저 허용한 뒤 선언 밖의 UFW 인바운드 허용 규칙을 지운다.
따라서 임시 공인 `22/tcp` 규칙도 사라진다. 뒤 단계가 실패해도 앞 단계 변경은 자동으로 롤백되지
않으므로 Tailscale 접속으로 원인을 고치고 전체 playbook을 재실행한다.

## 7. 성공 확인과 공인 SSH 제거

Tailscale로 서버에 접속해 확인한다.

```bash
sudo k3s kubectl get nodes
sudo k3s kubectl get applications -n argocd
sudo k3s kubectl get secretstores,externalsecrets -A
sudo k3s kubectl get certificates,clusterissuers -A
sudo ufw status verbose
```

완료 조건:

- `PLAY RECAP`: `failed=0`, `unreachable=0`
- K3s Node: `Ready`
- root Application: `Synced`, `Healthy`
- 필요한 SecretStore, ExternalSecret, Certificate, ClusterIssuer: 준비됨
- UFW: active, incoming deny, `tailscale0` 허용
- UFW: 출발지와 무관하게 `22/tcp`, `6443/tcp` port 허용 규칙 없음

여기까지 확인한 뒤 제공업체 방화벽/NSG의 임시 공인 `22/tcp` 허용을 제거한다. 마지막으로
Tailscale 주소로 새 OpenSSH 세션을 열어 접속이 계속 되는지 확인한다.

## 8. 운영과 문제 해결

### 재실행

Ansible 설정 변경이나 중간 실패 복구는 inventory가 Tailscale 주소를 가리키는 상태에서
`playbooks/bootstrap.yml` 전체를 다시 실행한다. 이미 등록된 서버에 enroll playbook을 반복 실행할
필요는 없다. 앱 image, Helm values, Kubernetes manifest 변경은 Git과 Argo CD로 처리한다.

### 자주 보는 실패

| 증상 | 먼저 확인할 것 |
|---|---|
| enroll inventory 거부 | `CHANGE_ME`, 공인 관리자 `/32`, `tailscale_enroll_confirm=true` |
| auth key 거부 | one-off key 사용 여부, `tag:umc-idc`, device approval 상태 |
| enroll 끝의 port 22 확인 실패 | 관리자 PC Tailscale 로그인, tailnet grant, 기존 서버 UFW |
| bootstrap이 Tailscale 연결을 거부 | `ansible_host`, `tailscale whois`, SSH 목적지 주소 |
| SSH host key 경고 | 제공업체 console의 실제 fingerprint와 서버 교체 여부 |
| UFW 규칙 때문에 중단 | deny/reject, route rule, quoted profile을 console에서 검토 |
| ESO 자격증명 누락 | [비밀값 관리](secrets.md)의 bootstrap key 준비 상태 |
| root health timeout | child Application, ExternalSecret, Certificate 상태 |

root 상태는 Secret 값을 출력하지 않는 다음 명령으로 조사한다.

```bash
sudo k3s kubectl describe application root -n argocd
sudo k3s kubectl get applications -n argocd
sudo k3s kubectl get secretstores,externalsecrets -A
sudo k3s kubectl get certificates,clusterissuers -A
```

### 방화벽 계약

| 경로 | UFW | 실제 접근 제한 |
|---|---|---|
| 관리 접속 | `tailscale0` interface 허용 | tailnet grant: 관리자 → `tag:umc-idc`, `tcp:22`만 |
| 공인 HTTPS | 인터넷 → `443/tcp` | 제공업체 상위 방화벽, UFW, Traefik |
| K3s 내부 | Pod·Service CIDR 허용 | 단일 node 내부 통신 |
| 공인 SSH/API | `22/tcp`, `6443/tcp` 규칙 없음 | 제공업체 방화벽에서도 차단 |

UFW 기본 정책은 inbound deny, outbound allow다. `common`은 필요한 규칙을 먼저 추가한 뒤 선언하지
않은 인바운드 `allow`·`limit` 규칙을 제거한다. 자동 해석하기 위험한 inbound deny/reject,
route allow/limit, quoted application profile을 발견하면 변경 전에 중단한다.

Route53은 DNS 서비스라 사용자 트래픽을 중계하는 source CIDR이 없다. 모바일·웹 사용자는
DNS가 돌려준 IDC public IPv4의 `443/tcp`로 직접 연결하므로 제공업체 상위 방화벽과 UFW가
public HTTPS를 허용해야 한다. `80/tcp`는 DNS-01 인증에 필요하지 않아 열지 않는다.
별도 trusted proxy가 없는 동안 Traefik은 외부가 임의로 보낸 `X-Forwarded-*` header를 신뢰하지 않는다.

### K3s 보호

K3s는 Secret 저장 암호화를 사용하고 kubeconfig를 `root:root`, mode `0600`으로 둔다. kubeconfig를
관리자 PC로 복사하지 않는다.

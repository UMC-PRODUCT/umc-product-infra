# Ansible로 단일 노드 K3s 초기 구성

관리자는 공인 SSH에 **개인 Linux 계정과 개인 공개키**로 접속한다. 백엔드 사용자는 같은 SSH
서버를 DB 터널로만 사용한다. Tailscale은 더 이상 설치·가입 조건이 아니다.

전체 실행 명령은 [Ansible README](../../ansible/README.md)를 따른다. 여기서는 접근 권한,
전환 검증과 실패 시 복구 조건을 설명한다.

> [!CAUTION]
> 기존 관리자 세션과 제공업체 console을 확보한다. 개인 관리자 공인 SSH·sudo를 확인하기 전에
> root 접속이나 기존 Tailscale을 끊지 않는다. `bootstrap.yml`은 UFW 인바운드 `allow`·`limit`
> 규칙을 소유하며, 중간 실패가 이전 단계를 자동으로 되돌리지는 않는다.

## 사전 준비와 두 단계 전환

- 관리자 PC: Python 3.12 이상, OpenSSH, 본인 private key
- 서버: Ubuntu 22.04/24.04 x86_64, Python 3, 현재 관리자 SSH와 sudo
- 복구: 제공업체 console 및 서버 SSH host key fingerprint
- 네트워크: 고정 공인 IP, 제공업체 방화벽의 TCP 22·443 허용, outbound DNS·HTTPS
- 표준 bootstrap: Git revision/CI, AWS Secrets Manager source, ESO key, Route 53 위임·IAM 준비

카페24 외부 방화벽은 이 playbook이 변경하지 않는다. 서버 UFW의 22번을 열어도 상위 방화벽이
막으면 접속되지 않는다. 팀원의 출발지 IP는 고정하지 않으며 키 인증과 계정별 권한으로 통제한다.
DB `5432`와 Kubernetes API `6443`은 외부에 열지 않는다.

`ansible/inventories/idc/hosts.yml`은 로컬 전용이다. 비밀번호·private key 본문을 넣지 않고
SSH private key의 로컬 경로와 공개키만 사용한다.

| 입력 | 준비 단계 | 최종 단계 |
|---|---|---|
| `ansible_host` | 기존 접속 가능 주소 | 서버 공인 IP |
| `ansible_user` | 기존 관리자 | 선언한 개인 `admin` |
| `ssh_access_public_host` | 서버 공인 IP | 동일 |
| `ssh_access_users` | 검토한 개인 계정 목록 | 동일 목록 유지 |
| `ssh_access_confirm` | 대상·계정·복구 경로 검토 뒤 `true` | `true` |
| `ssh_access_finalize` | `false` | 독립 접속 검증 뒤 `true` |
| `bootstrap_confirm` | `false` | 전체 bootstrap을 할 때만 `true` |

1. `ssh-access.yml`을 `ssh_access_finalize: false`로 실행한다. 개인 계정·키와 공인 SSH를
   준비하되 기존 관리 경로를 보존한다.
2. 별도 terminal에서 공인 IP에 개인 관리자로 접속해 `sudo -n true`를 확인한다.
   fingerprint가 다르거나 새 SSH·sudo가 실패하면 여기서 중단한다.
3. DB가 준비된 환경은 아래 DataGrip 터널 성공과 미승인 목적지·shell 거부까지 확인한다.
4. inventory를 공인 IP·개인 관리자 계정으로 바꾸고 `ssh_access_finalize: true`로
   `ssh-access.yml`을 재실행한다. root SSH를 차단하고 Tailscale service/package를 제거한다.
5. 다시 새 개인 SSH 세션을 열어 검증한 뒤 이전 관리자 세션을 닫는다.
6. 빈 서버라면 개인 관리자 공인 SSH와 `ssh_access_finalize: true`로 표준 bootstrap을 실행한다.
   DB가 생성되기 전에는 팀원 DB 접속 검증이 끝났다고 표시하지 않는다.

Tailscale identity 파일은 복구용으로 보존하며 tailnet 계정/기기 자체를 삭제하지 않는다.
실패하면 유지 중인 관리자 세션 또는 제공업체 console에서 SSH 설정·UFW를 복구한다. 필요하면
보존한 identity로 기존 VPN을 재구성할 수 있지만, 이는 자동 롤백이나 즉시 접속을 보장하지 않는다.
DB/PVC·K3s를 초기화하거나 host key 검증을 끄는 방식으로 복구하지 않는다.

## 개인 계정과 DB 터널

### 권한과 inventory 입력

`ssh_access_users`는 아래 필드를 사용한다.

| 필드 | 용도 |
|---|---|
| `name` | 개인별 Linux 사용자명. 공용 root 계정을 나누지 않는다. |
| `role` | `admin` 또는 `db_tunnel` |
| `state` | `present` 또는 명시적 회수용 `absent` |
| `public_keys` | 그 사람의 공개키 문자열 목록. private key를 넣지 않는다. |
| `permit_open` | `db_tunnel`이 접근할 실제 Service ClusterIP와 `:5432` 목록 |

`admin`은 shell과 비밀번호 없이 sudo를 사용할 수 있는 **root에 준하는 관리 권한**이다.
실제 운영 담당자에게만 부여한다. 일반 조회는 Grafana/Argo CD 조회 계정으로 충분할 수 있다.

`db_tunnel`은 지정된 DB 목적지로 local TCP forwarding만 가능하고 shell·명령 실행·sudo와
원격 forwarding을 허용하지 않는다. SSH 키가 있어도 DB 사용자명/비밀번호와 SQL 권한은
별도로 필요하다. 이 playbook은 PostgreSQL의 개인 role을 자동 생성하거나 변경하지 않는다.
prod 기본 권한은 조회 전용으로 두고 필요한 사람에게만 별도 승인으로 쓰기 권한을 부여한다.

팀원은 본인 컴퓨터에서 암호가 걸린 키를 생성하고 `.pub`만 운영자에게 보낸다.
아래 파일이 이미 있으면 덮어쓰지 말고 기존 키를 사용하거나 새 이름을 선택한다.

```bash
ssh-keygen -t ed25519 -f ~/.ssh/umc_idc_ed25519 -C "본인이메일"
cat ~/.ssh/umc_idc_ed25519.pub
```

### DataGrip 접속

인프라 관리자는 서버에서 DB Service 주소를 **현재 값으로** 조회한다. Secret 값은 조회하지 않는다.

```bash
sudo -n k3s kubectl -n db get service postgres -o jsonpath='{.spec.clusterIP}{"\\n"}'
sudo -n k3s kubectl -n dev-db get service postgres -o jsonpath='{.spec.clusterIP}{"\\n"}'
sudo -n k3s kubectl -n preview get service postgres-preview -o jsonpath='{.spec.clusterIP}{"\\n"}'
```

조회한 IP는 해당 팀원의 `permit_open`과 DataGrip General Host에 **정확히 동일하게** 넣는다.
Service를 삭제·재생성하거나 새 클러스터로 이전하면 IP가 달라질 수 있으므로 다시 조회한다.
호스트 OS에서 `*.svc.cluster.local`을 해석한다고 가정하지 않는다.

| DataGrip 위치 | 값 |
|---|---|
| SSH/SSL → Use SSH tunnel | 활성화 |
| SSH Host / Port | 서버 공인 IP / `22` |
| SSH Username / Authentication | 본인 Linux 사용자 / Key pair |
| SSH Private key | 본인 컴퓨터의 private key 파일 |
| General Host / Port | 해당 DB Service의 현재 ClusterIP / `5432` |
| General User / Password | 별도 발급한 개인 PostgreSQL 계정 |
| General Database | prod `umc_product`, dev `umc_product_dev`, Preview `umc_product_pr<N>` |

SSH 연결과 실제 PostgreSQL `Test Connection`을 모두 확인한다. Preview DB는 해당 PR의
배포가 DB를 만든 뒤에만 존재하며, 종료/정리 전에 DataGrip 연결을 닫는다.
자세한 생명주기는 [Preview 환경](preview-environments.md)을 따른다.

운영자는 추가로 터널 계정에서 shell·명령 실행·원격 forwarding과 허용되지 않은 목적지가
거부되는지 검증한다. 포트에 TCP 연결만 되는 결과를 DB 로그인 성공으로 표시하지 않는다.

### 팀원 회수

1. 본인의 개인 DB role에 새 로그인 차단을 적용하고 기존 DB 세션을 종료한다.
   정확한 사람/환경을 대조해 공용 앱 role이나 다른 사용자의 세션을 중단하지 않는다.
2. `ssh_access_users`의 해당 항목을 `state: absent`로 바꾸고 개인 관리자로
   `ssh-access.yml`을 실행한다. 현재 실행 중인 관리자 자신은 삭제 대상으로 삼지 않는다.
3. SSH 로그인 차단·기존 SSH 세션/터널 종료·계정 제거와 재접속 거부를 확인한다.
4. DB 소유 object·grant와 남긴 home의 보관/삭제는 별도 검토한다.

inventory에서 항목을 단순히 지우거나 공개키만 제거하는 것은 완전한 회수가 아니다.
`absent`는 home을 보존하며, 개인 DB role 회수는 별도다. 부여했던 Grafana/Argo CD/GitHub
권한도 실제 사용 범위에 맞춰 회수한다.

키 유출 대응, `admin`에서 `db_tunnel`로 권한 축소, 허용 DB 목적지 축소도 이미 열린 SSH 연결의
권한을 소급 변경하지 않는다. 다른 개인 관리자가 대상 사용자의 기존 세션을 종료하고 새 연결의
권한을 검증한다. 현재 작업 중인 관리자 자신의 세션을 끊어 유일한 관리 경로를 잃지 않도록 한다.

## 표준 bootstrap 검증

관리자 PC의 `ansible/`에서 실행한다.

```bash
ansible-playbook --syntax-check playbooks/ssh-access.yml
ansible-playbook --syntax-check playbooks/bootstrap.yml
ansible-lint playbooks/bootstrap.yml playbooks/ssh-access.yml
ansible-playbook --check --tags preflight playbooks/bootstrap.yml
```

저장소 루트에서 `./scripts/validate.sh`를 실행하고 skip·실패를 확인한다. 빈 서버의 전체
`--check`가 설치 전후 상태를 완전히 모사하지는 않는다.

`external_secrets_bootstrap`은 `ESO_AWS_ACCESS_KEY_ID`, `ESO_AWS_SECRET_ACCESS_KEY`를
private prompt 또는 승인된 비밀 저장소의 환경변수로 받는다. bootstrap 재실행에도 현재 값이
필요하며 inventory·Git·명령행 인자에 넣지 않는다.

```bash
ansible-playbook playbooks/bootstrap.yml
```

표준 bootstrap은 개인 관리자 공인 SSH 검증 뒤 `ssh_access`, `common`, `k3s`,
`argocd`, `external_secrets_bootstrap`, root Application 순서로 진행한다.
접속 계정/공개키만 변경할 때는 전체 bootstrap 대신 `ssh-access.yml`을 사용한다.

서버에서 다음 읽기 전용 상태를 확인한다.

```bash
sudo -n k3s kubectl get nodes
sudo -n k3s kubectl get applications -n argocd
sudo -n k3s kubectl get secretstores,externalsecrets -A
sudo -n k3s kubectl get certificates,clusterissuers -A
sudo -n ufw status verbose
sudo -n sshd -t
```

완료 기준은 Ansible 실패/접속불가 0, Node Ready, 필요한 Argo Application Synced/Healthy,
SecretStore·ExternalSecret·Certificate 준비, 개인 관리자 새 SSH·sudo 성공, root/비밀번호
로그인 거부와 DB 터널 권한 분리다. 외부 5432·6443이 계속 비공개인지도 확인한다.

## 운영과 문제 해결

### Argo CD 로컬 계정

Argo CD core 계정·권한은 [bootstrap Helm values](../../ansible/roles/argocd/files/argo-cd-values.yaml)에서
관리한다. `admin`은 운영자만 사용하고 공유하지 않는다. 팀 공용 `umc-viewer`는 로그인과
`role:readonly` 조회만 허용하며, 배포·설정 변경 권한과 API token 발급 권한은 주지 않는다.
익명 접근과 로그인 사용자의 기본 권한은 끈다. 공개 Ingress는 core chart가 아니라 별도
`argocd-access` Application이 [DNS·TLS 계약](domains-tls.md#argo-cd-공개-https)에 따라 관리한다.

로컬 계정에는 MFA나 GitHub 팀 연동이 없다. 공유 계정은 사용자를 개인별로 구분하거나 회수할 수
없고, 조회 전용이어도 자기 비밀번호는 변경할 수 있다. 공유 대상 변경·유출 시 운영자가 비밀번호를
회전하고 승인된 비밀 전달 수단으로 다시 전달한다. 기존 admin은 계정·권한 검증과 복구에 사용한다.

계정 정의와 RBAC만 Git에 저장한다. 비밀번호는 runtime `argocd-secret`에서 별도 관리하며
평문·해시 모두 values, Git, shell 인자, 로그에 넣지 않는다. 로그인한 운영자는 Argo CD CLI의
대화형 입력으로 `argocd account update-password --account umc-viewer`를 실행할 수 있다.
현재 비밀번호 질문에는 로그인한 운영자 계정의 비밀번호를 입력한다.

반영 전 고정 chart의 렌더와 diff를 검토하고, 반영 후 admin 로그인, viewer의 조회 성공과
변경 권한 거부, 기존 health gate 보존을 확인한다. 계정만 바꾸기 위해 전체 bootstrap을 재실행하거나
runtime Secret을 Git manifest로 덮어쓰지 않는다.

### Argo CD 공개 접속과 복구

일반 접속은 `https://argo.university.neordinary.com`을 사용한다. 공용 조회 계정으로 CLI에
로그인할 때는 `argocd login argo.university.neordinary.com --grpc-web --username umc-viewer`를
실행하고 비밀번호를 대화형으로 입력한다. TLS는 Traefik에서 종료하며 외부 `80/tcp`는 열지 않는다.

DNS·Ingress 장애 시 운영자는 개인 관리자 공인 SSH로 IDC에 접속해 loopback에만 HTTP를 연다.
core의 `server.insecure=true` 적용 후에는 backend가 HTTP이므로 이 복구 경로에 HTTPS를 쓰지 않는다.

```bash
# IDC의 SSH 세션에서 유지한다.
sudo k3s kubectl -n argocd port-forward --address 127.0.0.1 service/argocd-server 18080:80
```

관리자 PC의 별도 terminal에서 같은 IDC로 SSH tunnel을 유지한다. 아래 두 변수에는 승인된
개인 관리자 SSH 사용자와 서버 공인 주소를 사용한다.

```bash
ssh -N -L 127.0.0.1:18080:127.0.0.1:18080 "$IDC_SSH_USER@$IDC_NODE_HOST"
```

다른 관리자 PC terminal에서 `argocd login 127.0.0.1:18080 --plaintext --username admin`으로
복구 작업을 수행한다. `--plaintext`는 SSH로 암호화된 이 loopback 경로에만 사용한다.
복구 후 port-forward와 tunnel을 종료한다. Kubernetes API나 `--address 0.0.0.0`을
열어 우회하지 않는다. Helm 배포 성공만으로 인증 검증이 끝나지는 않으므로 공개 로그인과
viewer의 변경 권한 거부를 다시 확인한다.

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
| DB 터널만 실패 | `permit_open`의 IP/포트, Service ClusterIP, Endpoint/Pod, 개인 DB 계정 |
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

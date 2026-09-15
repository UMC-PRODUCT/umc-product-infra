# 인프라 팀 운영 가이드

운영 중인 Cafe24 단일 노드의 접근 권한과 일상 점검을 맡는 인프라 담당자용 문서다.
현재 운영 inventory는 Git에서 제외된 `ansible/inventories/idc-new/hosts.yml`이다.
계정 변경은 `ssh-access.yml`, 앱·Helm values·manifest 변경은 Git과 Argo CD로 처리한다.
빈 서버 설치는 [Ansible bootstrap](ansible-bootstrap.md), 서버 이전은
[Cafe24 이전 runbook](../../runbooks/cafe24-migration.md)을 따른다.

## 개인 계정과 DB 터널

| 역할 | 허용 범위 |
|---|---|
| `admin` | shell과 비밀번호 없는 sudo. root에 준하므로 승인된 인프라 운영자에게만 부여 |
| `db_tunnel` | `permit_open`에 지정한 DB로 local TCP forwarding만 허용. shell·명령 실행·sudo·원격 forwarding 차단 |

SSH 계정과 PostgreSQL 계정은 별개다. SSH 등록으로 DB 계정이나 SQL 권한이 생기지 않는다.
prod 조회 목적에는 읽기 전용 DB 계정을 제공하고, 쓰기 권한은 업무 범위를 확인해 별도로 승인한다.
Grafana·Argo CD 조회만 필요하면 SSH `admin`을 발급하지 않아도 된다.

## 새 팀원 SSH 계정 등록

개인 관리자 공인 SSH 구성이 완료된 운영 서버에 사용자를 추가하는 절차다.
`ssh_access_finalize: true`를 유지하며 처음부터 bootstrap하지 않는다.

### 1. 공개키와 필요한 권한 확인

팀원은 본인 컴퓨터에서 아래 파일의 존재 여부를 먼저 확인한다. 이미 있으면 덮어쓰지 않고
기존 키를 사용하거나 새 파일명을 선택한다. 새 키에는 Passphrase를 설정한다.

```bash
ssh-keygen -t ed25519 -f ~/.ssh/umc_idc_ed25519 -C "본인이메일"
cat ~/.ssh/umc_idc_ed25519.pub
```

관리자에게 원하는 SSH 계정명과 `.pub` 공개키 전체 한 줄, 필요한 DB 환경을 전달한다.
계정명은 `jimin`처럼 영문 소문자로 시작하는 2~31자로, 영문 소문자·숫자·밑줄만 사용한다.
개인키와 Passphrase는 본인 컴퓨터에만 두며 전달하지 않는다.

### 2. 기존 계정 목록에 추가

관리자는 [현재 DB Service IP](#datagrip-접속)를 조회하고, 운영 inventory의
`ssh_access_users` 목록에 다음 항목을 추가한다. 아래는 전체 inventory가 아니라 추가 항목의 예시다.
공개키·주소를 실제 값으로 바꾸고 승인하지 않은 환경은 `permit_open`에서 제외한다.

```yaml
          - name: jimin
            role: db_tunnel
            state: present
            public_keys:
              - "CHANGE_ME_ED25519_PUBLIC_KEY"
            permit_open:
              - "CHANGE_ME_DEV_DB_SERVICE_CLUSTER_IP:5432"
              - "CHANGE_ME_PROD_DB_SERVICE_CLUSTER_IP:5432"
```

`admin` 등록은 `role: admin`으로 지정하고 `permit_open`은 생략한다.
기존 `ansible_host`, 개인 관리자 `ansible_user`, 관리자 키 경로, `ssh_access_confirm: true`,
`ssh_access_finalize: true`와 방화벽 설정을 유지한다. 운영 inventory를 예제로 덮어쓰지 않는다.

> [!CAUTION]
> `ssh_access_users`는 **전체 허용 계정 목록**이다. 기존 관리자·팀원을 빼면 `AllowUsers`에서
> 제외되어 새 SSH 접속이 막힌다. 각 계정의 `public_keys`도
> `/etc/ssh/authorized_keys/<계정명>` 전체를 교체하므로 기존 승인 키를 모두 보존한다.

### 3. 관리자 PC에서 적용

기존 관리자 SSH 세션과 Cafe24 console 복구 경로를 확보한다. 수정 전 inventory 사본은
Git 밖의 승인된 보관 위치에 둔다. 로컬 Ansible 도구가 없는 PC는
[도구 준비](ansible-bootstrap.md#로컬-도구와-새-서버-inventory-준비)의 도구 설치 명령만 먼저 실행한다.
다음은 저장소 루트에서 시작하며, 대상이 `umc-cafe24-01` 한 대인지 확인한다.

```bash
cd ansible
source .venv/bin/activate

ansible -i inventories/idc-new/hosts.yml \
  k3s_servers --limit umc-cafe24-01 --list-hosts

ansible-playbook -i inventories/idc-new/hosts.yml \
  --syntax-check playbooks/ssh-access.yml

ansible-playbook -i inventories/idc-new/hosts.yml \
  --limit umc-cafe24-01 playbooks/ssh-access.yml
```

`PLAY RECAP`에서 `failed=0`, `unreachable=0`을 확인한다. 이 playbook은 계정·키와 함께
SSH 정책 및 UFW 규칙도 재적용하며 선언하지 않은 인바운드 허용 규칙을 제거할 수 있다.
실패하면 기존 관리자 세션을 유지하고 복구한다. 계정 변경에 `bootstrap.yml`을 실행하지 않는다.
Git 커밋·푸시만으로는 서버 계정이 생성되지 않는다.

### 4. 검증 후 접속 정보 전달

관리자는 별도 새 연결에서 기존 개인 관리자 로그인과 `sudo -n true` 성공을 확인한다.
팀원에게 서버 공인 IP(`ssh_access_public_host`), 개인 SSH 계정명, 승인된 DB Service IP와
별도 DB 접속 정보를 전달하고 아래 [DataGrip 설정](#datagrip-접속)으로 실제 DB 연결을 검증한다.
비밀번호는 승인된 비밀 전달 수단으로 제공하며 Git·채팅방에 남기지 않는다.

`db_tunnel`의 shell 접속 거부는 정상이다. 실제 DB `Test Connection` 성공과 shell·명령 실행·
원격 forwarding·미승인 목적지 연결 거부를 확인해야 등록이 끝난다.

## DataGrip 접속

인프라 관리자가 개인 `admin` SSH로 서버에 접속해 현재 Service ClusterIP를 조회한다.
다음 명령은 Secret 값을 읽지 않는다.

```bash
# prod
sudo -n k3s kubectl -n db get service postgres -o jsonpath='{.spec.clusterIP}{"\n"}'
# dev
sudo -n k3s kubectl -n dev-db get service postgres -o jsonpath='{.spec.clusterIP}{"\n"}'
# Preview
sudo -n k3s kubectl -n preview get service postgres-preview -o jsonpath='{.spec.clusterIP}{"\n"}'
```

조회한 IP와 `:5432`를 해당 사용자의 `permit_open`에 넣는다. 현재 SSH role은
`10.43.x.x:5432` 형식의 정확한 목적지만 허용한다. Service를 재생성하거나 클러스터를 이전하면
IP를 다시 확인한다. 서버 OS에서 `*.svc.cluster.local`을 해석한다고 가정하지 않는다.

| DataGrip 위치 | 입력 |
|---|---|
| SSH/SSL → Use SSH tunnel | 활성화 |
| SSH Host / Port | inventory의 `ssh_access_public_host` / `22` |
| SSH Username / Authentication | 본인 Linux 계정명 / Key pair |
| SSH Private key | 본인 PC의 `~/.ssh/umc_idc_ed25519` (`.pub` 아님) |
| SSH Passphrase | 키를 만들 때 본인이 설정한 비밀번호 |
| General Host / Port | `permit_open`과 동일한 현재 DB Service ClusterIP / `5432` |
| General User / Password | 승인된 환경별 PostgreSQL 계정·비밀번호 |
| General Database | prod `umc_product`, dev `umc_product_dev`, Preview `umc_product_pr<N>` |

SSH 연결뿐 아니라 실제 PostgreSQL `Test Connection`을 확인한다. Preview DB는 해당 PR의
배포가 DB를 만든 뒤에만 존재하며 종료·정리 전에 DataGrip 연결을 닫는다.
[Preview 환경](preview-environments.md)에서 DB 생명주기를 확인한다.

## 팀원 회수

1. 해당 사람과 환경, 사용한 DB role을 확인한다. 개인 PostgreSQL role이 있으면 새 로그인을
   차단하고 해당 사람의 DB 세션을 종료한다. 공용 앱·읽기 전용 role을 일괄 차단하거나
   다른 팀원의 DB 세션을 종료하지 않는다.
2. 운영 inventory에서 해당 SSH 항목을 `state: absent`로 바꾼다. 다른 개인 관리자와 승인 키를
   보존하며, 현재 Ansible을 실행하는 관리자 자신은 삭제 대상으로 삼지 않는다.
3. [관리자 PC 적용 명령](#3-관리자-pc에서-적용)으로 대상·문법을 확인한 뒤
   `playbooks/ssh-access.yml`을 실행한다. 전체 bootstrap은 필요하지 않다.
4. 새 SSH 로그인 거부, 기존 SSH 세션·터널 종료와 Linux 계정 제거를 확인한다. 개인 DB role을
   회수했다면 DB 재접속 거부도 확인한다. 공용 DB 자격증명을 알고 있던 팀원이라면 노출 범위에
   따라 회전을 검토하고 [Secret 운영](secrets.md)의 소비자별 순서를 따른다.
5. Grafana·Argo CD·GitHub 권한도 회수하고 남은 home, 개인 DB role의 소유 object와 grant 처리를 별도로 검토한다.

`state: absent`는 SSH 키를 제거하고 해당 사용자의 실행 중인 process·터널을 종료한 뒤
Linux 계정을 삭제하며 home은 보존한다. PostgreSQL role은 자동 변경하지 않는다.
inventory에서 항목만 지우거나 키만 삭제하는 것은 완전한 회수가 아니다.

키 교체·유출 대응, `admin`에서 `db_tunnel`로 축소, 허용 목적지 축소는 이미 열린 연결의
권한을 소급 변경하지 않는다. 다른 개인 관리자가 대상 사용자의 기존 세션을 종료하고
새 연결 권한을 확인한다. 유일한 관리자 세션을 끊지 않도록 한다.

## 일상 점검과 작업별 문서

클러스터 조회는 개인 관리자 SSH 세션에서 `sudo -n k3s kubectl`로 실행하며 kubeconfig를
노트북에 복사하지 않는다. Node `Ready`, 필요한 Application `Synced/Healthy`,
SecretStore·ExternalSecret·Certificate 준비 상태를 확인한다. 보안 업데이트와 SSH 인증 로그도 점검한다.
공인 SSH `22/tcp`·HTTPS `443/tcp`만 열고 DB `5432/tcp`·Kubernetes API `6443/tcp`는 외부에 공개하지 않는다.

- 관측·팀원 Grafana 계정: [모니터링 접근](monitoring-access.md), [대시보드 안내](../../observability/README.md)
- Argo CD: [공개 접속과 복구](ansible-bootstrap.md#argo-cd-공개-접속과-복구)
- 자격증명·DNS: [Secret 운영](secrets.md), [DNS와 TLS](domains-tls.md)
- 데이터·서버 작업: [Backup 활성화](../../runbooks/backup-activation.md),
  [Cafe24 이전](../../runbooks/cafe24-migration.md), [모니터링 이전](../../runbooks/monitoring-operator-migration.md)

작업별 전체 문서는 [문서 목차](../README.md)에서 찾는다. runbook의 검증 순서와 중단 조건을
따르며, 일상 장애 대응을 이유로 DB/PVC나 K3s를 초기화하지 않는다.

# Cafe24 단일 노드 이전

대상은 public IPv4 `1.255.226.166`, CPU 8코어, RAM 32GB, SSD 500GB인 새 Cafe24 서버다.
이 문서는 실행 승인이나 현재 배포 상태를 대신하지 않는다. 작업 창, 중단 허용 시간, 이전 대상과
단계별 담당자를 확정한 승인된 작업에서만 실행한다. GitHub SSO·배포팀·OAuth App 준비와
관리자 계정 비활성화는 별도 인증 작업이며, 여기서 완료됐다고 가정하지 않는다.

## 1. 대상과 복구 자료 확정

- 기존 `ansible/inventories/idc/hosts.yml`은 기존 서버용으로 보존한다. 새 서버에는
  `ansible/inventories/idc-new/hosts.yml`만 사용한다. 두 inventory가 서로 다른 서버인지 확인한다.
- 기존 Git revision, image digest, Node UID, DB 목록·크기·owner, PVC/PV·mount, controller·앱
  replica, CronJob/Job, DNS A/TXT·TTL·ownership을 운영 기록에 남긴다.
- prod·dev·존재하는 Preview DB는 모두 보존한다. 폐기가 허용된 것은 모니터링 이력뿐이다.
  DB/PVC/PV 삭제, K3s uninstall, 디스크 초기화, 자동 rollback은 이 절차에 없다.
- 두 노드 밖에 복구 가능한 backup을 확보한다. 실제 DB 버전·extension과 도구 호환성을 확인한다.
  Git 기준은 PostgreSQL 18/PostGIS 3.6이며 이전 과정에 DB 업그레이드를 섞지 않는다.
- OS·DB·dump·restore·관측 데이터의 실제 여유 용량을 확인한다. SSD 500GB 표기나
  `local-path` PVC 요청 크기는 실제 여유 공간 또는 quota를 보장하지 않는다.
- 이전 창에는 앱 image 변경, DB migration, Preview PR/label 변경과 수동 배포를 동결한다.
  비밀값·dump 내용은 Git·로그·채팅에 남기지 않고, Secret 값이나 kubeconfig를 조회·복사하지 않는다.

이하 명령은 저장소의 `ansible/`에서 실행한다. 대상 inventory 생략은 금지한다.
`inventories/idc-new/hosts.example.yml`을 같은 디렉터리의 `hosts.yml`로 복사하되 기존 파일은
덮어쓰지 않는다. 새 SSH 사용자·공개키·console·관리자 `/32`를 검토하고 최초 등록 때만
`tailscale_enroll_confirm: true`로 설정한다.

```bash
ansible-playbook -i inventories/idc-new/hosts.yml playbooks/tailscale-enroll.yml
```

등록 뒤 새 inventory의 `ansible_host`를 새 서버 Tailscale IP/MagicDNS로 바꾸고 host key를
대조한다. `tailscale_enroll_confirm: false`, `bootstrap_confirm: true`로 전환한 후 연결을 검증한다.

```bash
ansible -i inventories/idc-new/hosts.yml k3s_servers -m ansible.builtin.ping
ansible -i inventories/idc-new/hosts.yml k3s_servers --become -m ansible.builtin.ping
ansible-playbook -i inventories/idc-new/hosts.yml --syntax-check playbooks/bootstrap.yml
ansible-playbook -i inventories/idc-new/hosts.yml --check --tags preflight playbooks/bootstrap.yml
```

## 2. 새 서버 Core만 설치

새 inventory에 `bootstrap_root_app_enabled: false`를 유지한다. 이 값은 **최초 root 생성만
보류**한다. Application/ApplicationSet이 있으면 중단하며 기존 앱 정지용으로 사용하지 않는다.
AWS source와 secret-zero는 기존 승인된 경로로 준비한다.

```bash
ansible-playbook -i inventories/idc-new/hosts.yml playbooks/bootstrap.yml
ansible -i inventories/idc-new/hosts.yml k3s_servers --become -m ansible.builtin.command -a '/usr/local/bin/k3s kubectl get applications,applicationsets -A -o name'
```

마지막 결과가 비어 있고 K3s Node·Argo CD가 준비됐는지 확인한다. 공인 22/6443을 닫기 전에
Tailscale OpenSSH와 console 복구 경로를 검증한다. 아직 정상 root를 적용하지 않는다.

## 3. 기존 controller 동결 후 IP PR 반영

기존 workload 이름·replica를 대조하고 아래 이름과 다르면 중단한다.
root만 auto-sync 해제해도 자식 Application은 계속 동작하므로,
기존 Argo CD의 두 reconciler와 ExternalDNS를 모두 중지한다. 기존 API는 이 단계까지 유지한다.

```bash
ansible -i inventories/idc/hosts.yml k3s_servers --become -m ansible.builtin.command -a '/usr/local/bin/k3s kubectl -n argocd scale statefulset/argocd-application-controller --replicas=0'
ansible -i inventories/idc/hosts.yml k3s_servers --become -m ansible.builtin.command -a '/usr/local/bin/k3s kubectl -n argocd scale deployment/argocd-applicationset-controller --replicas=0'
ansible -i inventories/idc/hosts.yml k3s_servers --become -m ansible.builtin.command -a '/usr/local/bin/k3s kubectl -n external-dns scale deployment/external-dns --replicas=0'
```

실제 replica와 실행 Pod가 0이며 진행 중인 sync가 없는지 확인한 뒤에만 Cafe24 IP/resource PR을
merge한다. CI 성공과 merge revision을 기록한다. 기존 controller를 재시작하거나 기존 inventory로
bootstrap을 재실행하지 않는다. 아직 DNS는 기존 주소여야 하며 수동 A/TXT 변경도 하지 않는다.
merge 전 최신 `main`으로 rebase하고 모든 Ingress target과 ExternalDNS filter를 다시 검색한다.
별도 인증 작업에서 공개 Argo Ingress가 추가됐다면 그 target도 새 IP로 맞춘 뒤 재검증한다.

## 4. 새 서버 Prepare root

새 inventory의 두 값을 바꾼다. role이 지원하는 `argocd_root_app_manifest_path` override는
이후 bootstrap 재실행에서도 현재 단계의 경로를 유지해야 한다.

```yaml
bootstrap_root_app_enabled: true
argocd_root_app_manifest_path: "{{ playbook_dir }}/../../bootstrap/root-app-prepare.yaml"
```

```bash
ansible-playbook -i inventories/idc-new/hosts.yml playbooks/bootstrap.yml
```

`root-app-prepare.yaml`은 prod/dev API Application, Preview ApplicationSet, ExternalDNS를
제외한다. DB·SecretStore·ExternalSecret·관측 구성이 준비되고 이 네 대상이 생성되지 않았는지
확인한다. 새 DB에 업무 데이터가 이미 있으면 복원을 중단한다. backup CronJob도 정지 상태를 확인한다.
ExternalDNS 보류가 모든 외부 부작용을 막지는 않는다. Prepare에서도 cert-manager의 DNS-01 TXT
쓰기와 새 Alertmanager의 중복 Discord 알림이 가능하므로 기존·신규 인증서/알림 동작을 조율한다.

두 대체 root와 정상 root는 모두 `argocd/root` **한 개의 Application**이다. 동시에 별도 root를
만들지 않는다. `prune: false`이므로 exclude 추가나 Prepare로의 되돌림은 기존 앱을 멈추지 않는다.
root 경로 override를 지우고 bootstrap하면 정상 root가 적용되어 앱/DNS가 조기에 시작될 수 있다.

## 5. 쓰기 중단, 최종 dump, 복원

이 단계부터 서비스 쓰기 중단 시간이 시작된다. 다음 확인을 순서대로 완료하고 기록한다.

1. 기존 prod/dev API와 모든 Preview 앱, scheduler, 활성 Job, 수동 DB 세션 등 실제 writer를
   식별해 중지한다. CronJob suspend만으로 이미 실행 중인 Job은 멈추지 않는다. drain과 진행 중
   transaction 종료를 확인한다. 검증되지 않은 process가 남으면 dump 단계로 넘어가지 않는다.
2. 기존 DB별 최종 custom-format dump를 만든다. 대상 DB·환경·시점·크기·SHA-256을 기록하고,
   서버 밖 보호된 저장소로 전송한 뒤 checksum을 다시 대조한다. 각 파일은 소유자만 읽도록 한다.
   `pg_dump`는 DB 하나만 내보내므로 prod, dev, 실제 Preview DB 목록을 빠짐없이 대조한다.
3. 최종 dump 성공 후 기존 `db/postgres`, `dev-db/postgres`, 존재하는
   `preview/postgres-preview` StatefulSet을 replica 0으로 내리고 해당 DB Pod의 정상 종료와
   실제 DB container 중단을 확인한다. PVC는 유지한다. 외부 writer와 비-Kubernetes DB process도
   차단한다. 이것이 새 앱을 시작하기 전까지 유지할 **기존 DB 쓰기 차단**이다.
4. 새 서버의 비어 있는 대상 DB와 환경별 앱 owner를 확인하고 검증한 dump를 복원한다.
   `pg_restore --exit-on-error --no-owner --no-acl`과 해당 환경 role을 사용하는 복원 계획을
   사전에 검토한다. `--clean`, `DROP DATABASE`, PVC 삭제로 재시도하지 않는다. 실패·기존 데이터
   발견 시 중단하고 대상 DB/backup을 보존한다. role과 grant는 Git bootstrap 계약으로 재현한다.
5. 모든 DB의 schema·extension·Flyway 이력·sequence·핵심 table별 row count, owner/권한과
   앱 접속을 대조한다. Preview별 DB도 누락 여부를 확인한다. 개인정보 row 자체는 출력하지 않는다.
   checksum 일치나 `pg_isready` 성공만으로 복원 완료라고 판단하지 않는다.

`systemctl stop k3s`만으로는 실행 중인 container가 멈추지 않는다. 서비스 stop을 writer 차단의
증거로 쓰거나 uninstall/killall을 DB 종료 대신 자동 실행하지 않는다. [K3s 중지 동작](https://docs.k3s.io/upgrades/killall)

## 6. Verify root로 새 앱 점검

기존 DB 쓰기 차단과 최종 복원 검증을 확인한 뒤 새 inventory의 root 경로만 다음으로 교체한다.

```yaml
argocd_root_app_manifest_path: "{{ playbook_dir }}/../../bootstrap/root-app-verify.yaml"
```

```bash
ansible-playbook -i inventories/idc-new/hosts.yml playbooks/bootstrap.yml
```

Verify는 ExternalDNS만 제외한다. 현재 Preview는 `deployment.enabled`와 `ingress.enabled`가
false이므로 ApplicationSet 생성이 Preview workload 실행을 뜻하지 않는다. 이 gate나 미검증
image gate를 임의로 열지 않는다. DNS가 이전 주소여도 새 앱의 Flyway,
scheduler·외부 API 호출·데이터 정리 작업은 실행될 수 있다. 이 시점부터 새 데이터 변경을 추적한다.

새 IP의 production 인증서와 보호된 문서 경로를 점검한다. 아래 익명 요청은 `401`이어야 한다.
`-k`로 TLS 실패를 숨기지 않는다. 정상 문서 접근도 같은 `--resolve`에 `--user 문서사용자명`을
붙여 curl의 숨김 비밀번호 prompt로 확인한다. 비밀번호를 인자에 쓰지 않는다. DNS 전환 전의 일반
브라우저 접속은 기존 서버를 볼 수 있으므로 새 서버 검증의 증거로 쓰지 않는다.

```bash
curl --resolve api.university.neordinary.com:443:1.255.226.166 --silent --show-error -o /dev/null -w '%{http_code}\n' https://api.university.neordinary.com/docs/scalar.html
```

readiness는 Pod의 management port 9090에 있고 외부 Service의 8080에는 노출하지 않는다.
새 inventory를 명시해 별도로 확인한다.

```bash
ansible -i inventories/idc-new/hosts.yml k3s_servers --become -m ansible.builtin.command -a '/usr/local/bin/k3s kubectl -n app wait --for=condition=Ready pod -l app.kubernetes.io/name=umc-product-server --timeout=300s'
```

dev와 승인된 Preview host도 실제 배포 목록에 맞춰 점검한다. 새 앱의 readiness, DB 연결·업무
읽기/승인된 쓰기, dashboard·datasource, 익명 접근 차단과 사람별 로그인/권한을 검증한다.
인증 main 변경의 적용·SSO 검증이 끝나지 않았다면 관리자 비활성화 완료로 표시하지 않는다.

## 7. DNS를 마지막으로 전환

기존 Argo CD·ExternalDNS·DB writer가 여전히 멈췄는지 재확인한다. A/TXT snapshot, TTL,
`txtOwnerId=umc-infra-idc`, `_external-dns.` prefix와 zone 소유권을 대조한다. 충돌 시 중단한다.
최종 전환 승인 후에만 새 inventory의 root 경로를 정상 manifest로 교체한다.

```yaml
argocd_root_app_manifest_path: "{{ playbook_dir }}/../../bootstrap/root-app.yaml"
```

```bash
ansible-playbook -i inventories/idc-new/hosts.yml playbooks/bootstrap.yml
```

새 ExternalDNS 한 개만 시작해 Git의 새 IP로 A/TXT를 수렴시킨다. authoritative DNS와 외부
resolver, 기존 TTL 경과 뒤 HTTPS·인증·업무 기능을 다시 확인한다. 기존 서버에는 캐시된 DNS로
접속이 남아도 쓰기가 불가능해야 한다. 기존 DB/PVC·최종 dump는 보존하고 새 backup과 node 밖
restore까지 검증한 뒤 별도 폐기 승인을 받는다. 앱/DB 자동 복구·기존 writer 재시작은 하지 않는다.

## 중단과 복구 경계

| 실패 시점 | 중단 상태와 다음 결정 |
|---|---|
| 최종 dump 이전 | 새 앱/DNS를 보류한다. 기존 서비스 재개는 동결 범위·Git revision을 검토한 별도 결정이다. |
| 기존 DB 종료 후, 새 앱 시작 전 | 기존 DB/PVC와 dump를 보존한다. 원본의 쓰기 재개는 새 writer 부재를 검증한 뒤 승인한다. |
| Verify 또는 DNS 전환 이후 | 양쪽 writer를 동시에 켜지 않는다. 새 데이터 변경·외부 부작용을 평가하고 데이터 정합성 복구 계획을 승인받는다. DNS만 되돌리거나 이전 dump를 덮어쓰지 않는다. |

DB 접속은 [DB 접근 가이드](../docs/guides/db-access.md), Secret 준비는
[Secret 운영](../docs/guides/secrets.md), 후속 backup 검증은
[backup 활성화](backup-activation.md)를 따른다. 이 문서의 대상·중단 조건을 먼저 적용한다.

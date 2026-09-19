# Grafana·Argo CD 접근 운영

인프라 운영자가 계정을 발급·회수하고 공개 HTTPS와 복구 경로를 점검하는 절차다.
팀원의 로그인·조회 방법은 [모니터링 가이드](../guides/monitoring.md)를 본다.
실제 접속 주소와 로그인 정보는 승인된 비밀 전달 수단으로 전달한다. 이 문서의 `<grafana-host>`,
`<argo-host>`는 자리표시자이며 아래 명령을 실행할 때는 승인된 실제 주소를 사용한다.

공개 HTTPS는 [DNS·TLS 운영](domains-tls.md), 관측 stack 배포 구성은
[관측 스택 배포 README](../../argocd/applications/platform/observability/README.md)를 따른다.
저장소 선언과 실제 배포 상태를 구분하고 아래 검증을 마치기 전 공개 접속이 준비됐다고 판단하지 않는다.

## Grafana 계정 관리

Grafana는 자체 로그인을 사용한다. 익명 접근과 자체 회원가입은 끄고 다음 권한을 유지한다.

- 관리자 계정을 공유하지 않고 사람마다 별도 local account를 만든다.
- 대시보드 조회는 Organization role `Viewer`, dashboard 수정이 필요한 사람만 검토 후 `Editor`를 부여한다.
- `Admin`과 Server Admin은 계정·datasource 운영 담당자에게만 둔다.
- 퇴임·역할 변경 시 해당 사람의 계정을 비활성화하거나 삭제한다.
- `allow_sign_up: false`와 `auth.anonymous.enabled: false`를 유지한다.

Server Admin으로 로그인해 `Administration` → `Users and access` → `Users` → `New user`에서
계정을 만든다. 임시 password는 승인된 비밀 전달 수단으로 개인에게 보낸다. 생성 후 Grafana
Admin은 `No`, 기본 organization role은 `Viewer`인지 확인한다.

Viewer도 연결된 datasource에 직접 query할 수 있으므로 제품팀 구성원에게만 발급한다.
애플리케이션 log에는 token·password·개인정보를 남기지 않는다. local account에는 조직 SSO·MFA가
연결되어 있지 않으므로 필요해지면 별도 변경으로 검토한다.

초기 관리자 계정은 `/umc-product/platform/monitoring/grafana-admin` source의 `admin-user`,
`admin-password`에서 온다. 값을 Git·명령행 인자·로그·채팅에 남기거나 Kubernetes Secret 조회로
출력하지 않는다. Secret 준비·회전은 [Secret 운영](secrets.md)을 따른다.

Dashboard와 datasource는 Git에서 복원하지만 local user DB는 Grafana PVC에 있다.
노드 손실 후 팀원별 계정을 다시 만들 수 있도록 승인 사용자 목록을 비밀값이 아닌 운영 기록으로 관리한다.

## Grafana 공개 배포 검증

저장소는 Grafana Application 안에서 production Certificate wave `-2`가 최신 generation으로
`Ready=True`가 된 뒤 Ingress wave `1`을 적용하도록 선언한다. Route 53은 DNS만 제공하고
proxy·WAF·사용자 인증은 제공하지 않는다. 인증은 Grafana가 담당하며 Grafana Ingress는 API
Ingress와 독립적으로 관리한다.

1. Grafana target annotation이 승인된 IDC 고정 공인 IPv4이고 ExternalDNS의
   `--target-net-filter`가 같은 주소의 `/32`인지 확인한다.
2. `monitoring` namespace의 Grafana production Certificate가 승인된 hostname을 포함하며
   최신 generation에서 `Ready=True`인지 확인한다.
3. `root_url`, domain enforcement, secure cookie, basic auth, 익명 접근 off와 회원가입 off를 확인한다.
4. Ingress와 ExternalDNS의 exact A record 및 TXT ownership을 확인한다.
5. 외부에서 DNS 결과·certificate chain·HTTPS 로그인 화면을 확인하고 HTTP로 인증을 우회할 수 없는지 확인한다.
6. 팀원별 Viewer 계정을 만든 뒤 조회 성공과 관리 기능 접근 거부를 확인한다.
7. 개인 관리자 공인 SSH를 통한 local-only health와 제공업체 console 복구 경로를 확인한다.

실제 IP와 production TLS 없이 hostname만 만들거나 임시 HTTP Ingress를 열지 않는다.
TLS private key와 AWS access key도 Git에 저장하지 않는다.

### Grafana 내부 health 점검

개인 `admin` SSH 계정으로 IDC 노드에 접속해 loopback에만 port-forward를 유지한다.

```bash
sudo -n k3s kubectl port-forward --address 127.0.0.1 -n monitoring service/grafana 13000:80
```

관리자 PC의 별도 terminal에서 같은 IDC로 SSH tunnel을 유지한다. 변수에는 승인된 개인 관리자
사용자와 서버 공인 주소를 사용한다.

```bash
ssh -N -L 127.0.0.1:13000:127.0.0.1:13000 "$IDC_SSH_USER@$IDC_NODE_HOST"
```

다른 관리자 PC terminal에서 `GRAFANA_HOST`에 전달받은 실제 hostname을 지정하고 health만 확인한다.

```bash
GRAFANA_HOST='<grafana-host>'
curl -fsS -H "Host: ${GRAFANA_HOST:?실제 Grafana hostname 필요}" http://127.0.0.1:13000/api/health
```

canonical HTTPS URL, domain enforcement, secure cookie 때문에 HTTP port-forward는 브라우저
로그인에 쓰지 않는다. 팀원 UI는 공개 배포 검증 후 전달한 HTTPS 주소에서만 연다.
`--address 0.0.0.0`이나 `root_url`/cookie 설정 완화로 우회하지 않는다. 점검 후 port-forward와
tunnel을 종료한다.

## Argo CD 로컬 계정

Argo CD core 계정·권한은 [bootstrap Helm values](../../ansible/roles/argocd/files/argo-cd-values.yaml)에서
관리한다. `admin`은 운영자만 사용하고 공유하지 않는다. 팀 공용 `umc-viewer`는 로그인과
`role:readonly` 조회만 허용하며 배포·설정 변경 권한과 API token 발급 권한은 주지 않는다.
익명 접근과 로그인 사용자의 기본 권한은 끈다. 공개 Ingress는 core chart가 아니라 별도
`argocd-access` Application이 [DNS·TLS 계약](domains-tls.md#argo-cd-공개-https)에 따라 관리한다.

로컬 계정에는 MFA나 GitHub 팀 연동이 없다. 공유 계정은 사용자를 개인별로 구분하거나 회수할 수
없고 조회 전용이어도 자기 비밀번호는 변경할 수 있다. 공유 대상 변경·유출 시 운영자가 비밀번호를
회전하고 승인된 비밀 전달 수단으로 다시 전달한다. 기존 admin은 계정·권한 검증과 복구에 사용한다.

계정 정의와 RBAC만 Git에 저장한다. 비밀번호는 runtime `argocd-secret`에서 별도 관리하며
평문·해시 모두 values·Git·명령행 인자·로그에 넣지 않는다. 로그인한 운영자는 CLI의 대화형 입력으로
`argocd account update-password --account umc-viewer`를 실행할 수 있다.
현재 비밀번호 질문에는 로그인한 운영자 계정의 비밀번호를 입력한다.

반영 전 고정 chart의 렌더와 diff를 검토하고 반영 후 admin 로그인·viewer 조회 성공·변경 권한 거부와
기존 health gate 보존을 확인한다. 계정만 바꾸기 위해 전체 bootstrap을 재실행하거나 runtime
Secret을 Git manifest로 덮어쓰지 않는다.

## Argo CD 공개 접속과 복구

일반 접속은 전달받은 HTTPS 주소를 사용한다. CLI 조회가 필요하면 `ARGO_HOST`에 실제 hostname을
지정하고 비밀번호는 대화형으로 입력한다.

```bash
ARGO_HOST='<argo-host>'
argocd login "${ARGO_HOST:?실제 Argo CD hostname 필요}" --grpc-web --username umc-viewer
```

TLS는 Traefik에서 종료하며 외부 `80/tcp`는 열지 않는다. 공개 접속 전
[DNS·TLS 운영](domains-tls.md#argo-cd-공개-https)에 따라 Certificate·Ingress·DNS를 검증하고
admin 로그인·viewer 조회 성공·변경 권한 거부를 확인한다.

DNS·Ingress 장애 시 개인 관리자 공인 SSH로 IDC에 접속해 loopback에만 HTTP를 연다.
core의 `server.insecure=true` 적용 후 backend는 HTTP이므로 이 복구 경로에 HTTPS를 쓰지 않는다.

```bash
# IDC의 SSH 세션에서 유지한다.
sudo -n k3s kubectl -n argocd port-forward --address 127.0.0.1 service/argocd-server 18080:80
```

관리자 PC의 별도 terminal에서 같은 IDC로 SSH tunnel을 유지한다. 변수에는 승인된 개인 관리자
SSH 사용자와 서버 공인 주소를 사용한다.

```bash
ssh -N -L 127.0.0.1:18080:127.0.0.1:18080 "$IDC_SSH_USER@$IDC_NODE_HOST"
```

다른 관리자 PC terminal에서 `argocd login 127.0.0.1:18080 --plaintext --username admin`으로
복구 작업을 수행한다. `--plaintext`는 SSH로 암호화된 이 loopback 경로에만 사용한다.
복구 후 port-forward와 tunnel을 종료한다. Kubernetes API나 `--address 0.0.0.0`을 열어
우회하지 않는다. Helm 배포 성공만으로 인증 검증이 끝나지는 않으므로 공개 로그인과 viewer의
변경 권한 거부를 다시 확인한다. 복구 중 desired state를 변경했다면 Git에도 반영해 Argo CD로 수렴시킨다.

## 관측 stack 보호 경계

- 앱은 `otel-collector.monitoring.svc.cluster.local:4317/4318`에만 OTLP를 보낸다.
- Collector ingress는 `app`, `dev-app`, `preview` namespace의 앱 Pod만 허용한다.
- Grafana는 `kube-system`의 Traefik Pod에서 오는 `3000/TCP`만 별도 허용한다.
- Actuator 9090은 application Service나 Ingress에 노출하지 않는다.
- kube-state-metrics는 cluster 상태를 읽지만 Secret과 ConfigMap 내용은 수집하지 않는다.
- Prometheus는 14일 / 4GB 중 먼저 도달하는 조건, Tempo는 7일, Loki는 90일 retention을 사용한다.
- 단일 노드 local-path storage이므로 node loss 시 telemetry가 유실될 수 있다.
- 같은 노드의 Alertmanager는 node/power/network 전체 장애를 알릴 수 없다. 운영 전 IDC 밖에
  uptime monitor를 둔다.

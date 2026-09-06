# UMC 인프라 처음 읽는 가이드

이 문서는 `umc-infra`를 처음 보는 운영자가 **무엇부터 읽고, 각 도구가 왜 있으며,
어디까지 완료되어야 실제 서비스가 배포된 것인지** 이해하기 위한 안내서다.
실행 명령보다 구조를 먼저 설명한다. 배포 절차서가 아니므로 처음부터 끝까지 읽지 말고,
낯선 계층이나 용어가 있는 절만 찾아본다. 실제 준비 여부는 각 작업 가이드의 사전 조건과
검증 명령으로 확인한다.

## 먼저 기억할 한 문장

**사람이 서버·tailnet policy·AWS Route53·GitHub의 바깥 기반을 준비하고,
Ansible이 Tailscale 관리 경로로 빈 서버를 K3s 클러스터로 만들며, 그 뒤부터 Argo CD가
Git의 Kubernetes 선언을 계속 맞춘다.**

책임을 섞지 않은 이유는 장애가 났을 때 고칠 곳을 분명하게 하기 위해서다.

| 계층 | 담당 | 이 저장소의 위치 | 하지 않는 일 |
|---|---|---|---|
| 외부 기반 | VM/IDC·공인 IP·상위 방화벽·영속 디스크, Tailscale policy, AWS Route53, GitHub | 사람의 사전 작업 + `cloud/aws/` | Kubernetes workload 운영 |
| 서버 bootstrap | OS, Tailscale, OpenSSH/UFW, K3s, Argo CD, ESO 최초 자격증명 | `ansible/` | 앱 버전의 지속 배포 |
| GitOps | 클러스터 안의 선언 상태를 Git과 일치시킴 | `bootstrap/`, `argocd/` | 서버나 AWS 계정 생성 |
| workload | 앱, DB, DNS/TLS controller, 관측 스택 | `charts/`, `manifests/` | 실제 secret 값을 Git에 저장 |
| 운영 절차 | 점검·복구·검증 | `docs/`, `runbooks/`, `scripts/` | 선언 상태를 대신함 |

## 1. 전체 흐름

### 처음 한 번: 빈 서버를 GitOps 클러스터로 만들기

```text
관리자 PC
  ├─ 최초 1회: 임시 public SSH로 Tailscale enroll
  └─ Tailscale 경유 OpenSSH로 Ansible
      ├─ Ubuntu 서버의 OpenSSH·UFW·커널 설정
      ├─ K3s 설치
      ├─ Argo CD 설치
      ├─ ESO가 AWS에 접근할 최초 자격증명(secret-zero) 주입
      └─ root Application 등록
              ↓
          Argo CD가 Git main을 읽고 계속 reconcile
```

Ansible은 최초 설치 뒤에도 같은 설정을 다시 적용하는 복구 도구다. 하지만 앱 image를
업데이트할 때마다 Ansible을 실행하지는 않는다. 앱과 클러스터 리소스 변경은 Git에 반영하고
Argo CD가 가져가게 한다.

![GitOps 구성도](../diagrams/out/gitops-tree.png)

### 요청 한 번: 모바일·웹에서 백엔드까지

```text
사용자
  → Route53에서 host의 A record 조회
  → 조회한 서버 고정 공인 IPv4의 443/tcp로 직접 HTTPS
  → 제공업체 상위 방화벽(Azure에서는 NSG)
  → 서버 UFW
  → K3s Traefik Ingress
  → Kubernetes Service
  → umc-product-server Pod
  → PostgreSQL / AWS S3 / AWS SES
```

Route53은 DNS만 담당하고 사용자 요청을 대신 통과시키거나 차단하지 않는다. 제공업체 상위
방화벽과 UFW가 실제 서버의 public `443/tcp` 접근을 제한하며, Traefik과 앱 인증이 요청을 처리한다.

![요청 흐름](../diagrams/out/traffic-flow.png)

### 비밀값 한 개: 로컬 worksheet에서 Pod까지

```text
.env.prod / .env.dev / .env.preview
  → 일회성 업로더
  → AWS Secrets Manager
  → External Secrets Operator
  → namespace별 Kubernetes Secret
  → 애플리케이션 Pod 환경변수
```

`.env.*`는 배포 파일이 아니라 **로컬에서 값을 모으기 위한 임시 원장**이다. Git에 올리거나
서버로 복사하지 않는다. Git에는 AWS secret의 경로와 key 계약만 둔다.

![비밀값 공급 흐름](../diagrams/out/secret-supply-chain.png)

## 2. 필요한 부분 찾기

| 궁금한 것 | 볼 곳 |
|---|---|
| 전체 계층과 폴더 | 이 문서의 1절과 3절 |
| 왜 단일 K3s와 GitOps를 쓰는지 | [K3s 아키텍처 결정 기록](../architecture/k3s.md) |
| 빈 서버 설치 명령 | [Ansible README](../../ansible/README.md) |
| Argo CD 이후 생성되는 것 | `bootstrap/root-app.yaml`, `argocd/projects.yaml`, `argocd/applications/` |
| 환경별 애플리케이션 차이 | `charts/umc-product-server/values-*.yaml` |
| Secret 공급 경로 | [비밀값 관리](secrets.md), `charts/umc-secrets/` |
| DNS와 인증서 | [도메인과 TLS](domains-tls.md) |
| backup 활성화와 복원 연습 | `runbooks/` |

파일을 볼 때는 세부 값보다 먼저 **어느 계층이 소유하고 다음에 누가 소비하는지**를 찾는다.

## 3. 폴더별 역할과 존재 이유

### `ansible/`: 빈 Linux 서버를 안전하게 준비

Ansible은 Tailscale로 연결된 서버의 OpenSSH에 접속해 사람이 반복하기 어려운 초기
작업을 같은 순서로 수행한다. Tailscale SSH는 사용하지 않으며 기존 public key/PEM
인증을 계속 쓴다.

```text
ansible/
├── inventories/idc/hosts.yml       # 어느 서버에 접속할지; 로컬 전용
├── playbooks/tailscale-enroll.yml   # 최초 1회 tailnet에 등록
├── playbooks/bootstrap.yml          # tailnet 접속 후 전체 실행 순서
└── roles/
    ├── tailscale/                   # Tailscale 설치·연결 계약
    ├── common/                      # OS·SSH·UFW·swap·kernel
    ├── k3s/                         # K3s와 Traefik 설정
    ├── argocd/                      # Argo CD와 root Application
    └── external_secrets_bootstrap/  # ESO secret-zero
```

`hosts.yml`이 Git에서 제외되는 이유는 서버 주소와 로컬 접속 정보가 환경마다 다르기 때문이다.
secret은 이 파일에도 넣지 않는다.

### `bootstrap/`과 `argocd/`: Ansible에서 GitOps로 넘기는 경계

`bootstrap/root-app.yaml`은 Argo CD에 넣는 시작점이다. 이 Application이 `argocd/`를 읽으면
나머지 Application이 생성된다. 이를 app-of-apps 구조라고 부른다.

`argocd/projects.yaml`은 각 Application이 읽을 저장소와 쓸 namespace를 제한한다.
`argocd/applications/`는 실제 배포 묶음을 환경·플랫폼별로 선언한다.

```text
root Application
  ├─ platform: namespace, ESO, Reloader, cert-manager, ExternalDNS, observability
  ├─ prod: PostgreSQL + server
  ├─ dev: PostgreSQL + server
  └─ preview: 공유 PostgreSQL + PR별 server ApplicationSet
```

### `charts/`: 반복되는 앱 구성을 값만 바꿔 재사용

Helm chart는 Kubernetes YAML의 템플릿이다. `umc-product-server`의 Deployment·Service·Ingress·
NetworkPolicy를 한 번 정의하고 `values-prod.yaml`, `values-dev.yaml`, `values-preview.yaml`로
환경 차이만 준다. 같은 YAML을 환경마다 복사할 때 생기는 drift를 줄이기 위한 구조다.

`values`에 공개돼도 되는 값은 profile, URL, bucket 이름, 기능 flag 같은 런타임 설정이다.
비밀번호, private key, access key는 `umc-secrets`가 만든 Secret에서 주입한다.

### `manifests/`: 템플릿화 이득이 작은 선언

namespace, PostgreSQL, ClusterIssuer, NetworkPolicy처럼 구조 자체가 중요하거나 환경별 차이가
명확한 리소스는 raw YAML로 둔다. raw manifest라고 해서 수동으로 `kubectl apply`하는 것은
아니다. Argo CD가 이 경로도 Git에서 읽는다.

### `cloud/aws/`: 클러스터 밖 AWS 자원

여기에는 S3, SES, External Secrets용 IAM을 만드는 CloudFormation 템플릿이 있다.
CloudFormation은 “AWS 리소스용 선언 파일”이며, 파일을 저장하거나 Git에 push하는 것만으로는
AWS에 아무것도 만들어지지 않는다. 변경 집합을 검토한 뒤 AWS에 명시적으로 배포해야 한다.

Terraform을 쓰지 않더라도 S3/IAM/SES 구성을 콘솔 클릭 기록으로만 남기지 않기 위해 이 범위에
CloudFormation을 사용한다. Azure VM이나 Kubernetes 리소스까지 관리하는 도구는 아니다.

### `observability/`과 `manifests/observability/`

`observability/`가 사람이 수정하는 dashboard·alert 원본이고,
`manifests/observability/`는 스크립트가 Kubernetes ConfigMap으로 만든 결과다. 생성물을 직접
고치면 다음 생성 때 덮이므로 원본을 수정한 뒤 생성·검증한다.

### `docs/`, `runbooks/`, `scripts/`

- `docs/architecture/`: 결정과 전제. “왜”를 기록한다.
- `docs/guides/`: 정상 작업 순서. “어떻게”를 기록한다.
- `runbooks/`: backup 활성화처럼 데이터에 영향을 주는 작업의 명령과 중단 조건을 기록한다.
- `scripts/`: 반복 생성, 계약 검사, 일회성 AWS bootstrap을 자동화한다.

## 4. Ansible을 처음 볼 때 알아둘 것

| 용어 | 쉬운 뜻 | 이 저장소의 예 |
|---|---|---|
| controller | Ansible 명령을 실행하는 관리자 PC | 현재 노트북 |
| managed host | Tailscale 경유 SSH로 설정되는 대상 서버 | 현재 임시 Azure VM 또는 최종 IDC 한 대 |
| inventory | 접속 대상과 환경 입력 | `inventories/idc/hosts.yml` |
| playbook | 역할을 어떤 순서로 실행할지 | `playbooks/bootstrap.yml` |
| role | 한 책임의 task·기본값·template 묶음 | `roles/k3s/` |
| task | 패키지 설치, 파일 배치 같은 한 단계 | 각 role의 `tasks/main.yml` |
| template | 변수로 서버 설정 파일을 생성 | `roles/k3s/templates/` |
| handler | 관련 파일이 바뀔 때만 재시작 | 각 role의 `handlers/main.yml` |
| `become: true` | 대상 서버에서 sudo 권한 사용 | bootstrap play 전체 |
| idempotent | 같은 작업을 다시 해도 같은 상태로 수렴 | 재실행 가능한 task 설계 |

실제 순서는 다음과 같다.

```text
tailscale-enroll.yml을 최초 1회 실행
  → inventory의 ansible_host를 Tailscale MagicDNS/IP로 변경
  → ansible -m ping으로 tailnet SSH 검증
bootstrap.yml의 pre_tasks
  → inventory·현재 Tailscale 경유 OpenSSH 연결 검사
  → tailscale
  → common
  → k3s
  → argocd
  → external_secrets_bootstrap
  → root Application 적용
  → Argo CD Synced·Healthy 대기
```

중요한 점은 Ansible이 하나의 트랜잭션이 아니라는 것이다. 뒤 단계가 실패해도 앞에서 바꾼
UFW·SSH·K3s가 자동으로 원상복구되지는 않는다. 그래서 실행 전에 tailnet SSH와 제공업체 콘솔을
모두 검증하고, 실패하면 무작정 초기화하지 말고 원인을 고쳐 같은 playbook을 재실행한다.

tailnet policy의 관리 계약은 [policy 예시](../../ansible/tailscale-policy.example.hujson)처럼
관리자 주체에서 `tag:umc-idc`의 `tcp:22`만 grant하는 것이다. 서버 UFW에 공인
`22/tcp`와 Kubernetes API `6443/tcp` 허용 규칙을 두지 않는다.

## 5. Argo CD, Helm, Kubernetes의 관계

Argo CD 자체는 아직 controller가 없는 bootstrap 단계이므로 Ansible이 checksum으로 고정한
Helm release로 설치한다. 이후 앱과 platform chart는 Argo CD가 관리한다.

세 도구는 서로 대체 관계가 아니다.

- Kubernetes: 실제로 Pod·Service·Secret·Ingress를 실행하는 기반이다.
- Helm: 반복되는 Kubernetes YAML을 값에 따라 렌더링한다.
- Argo CD: Git과 현재 Kubernetes 상태의 차이를 감시하고 다시 맞춘다.

따라서 정상 운영 변경은 보통 다음 흐름이다.

```text
Git에서 values 또는 manifest 변경
  → PR 검증·승인·merge
  → Argo CD가 변경 감지
  → Helm render 또는 raw manifest 적용
  → Kubernetes가 새 상태로 수렴
```

긴급 진단을 위한 `kubectl` 조회는 가능하지만, live resource만 고치고 Git을 그대로 두면
Argo CD가 다시 Git 상태로 돌리거나 다음 담당자가 실제 상태를 알 수 없게 된다.

## 6. DNS와 TLS 도구를 구분하는 법

| 도구 | 질문으로 기억하기 | 하는 일 |
|---|---|---|
| Route53 | “이 host가 어느 public IP인지 어떻게 알려 줄까?” | authoritative DNS; proxy·cache·WAF는 하지 않음 |
| ExternalDNS | “Ingress가 생겼으니 DNS record도 만들까?” | exact A/TXT record 생성·삭제 |
| cert-manager | “이 host의 인증서를 발급·갱신할까?” | Let's Encrypt DNS-01과 TLS Secret |
| Traefik | “서버에 도착한 HTTPS를 어느 Service로 보낼까?” | K3s Ingress controller |

Preview는 ExternalDNS가 PR별 exact record를 만들고, cert-manager가 미리 발급한 wildcard
Certificate 하나를 함께 사용한다. DNS wildcard record를 만드는 구조와는 다르다.

## 7. ‘배포가 끝났다’의 단계

Ansible 명령 한 번이 성공했다고 서비스 배포가 끝난 것은 아니다.

| 단계 | 완료 기준 |
|---|---|
| 정적 검증 | Helm/YAML/Kubernetes schema/CloudFormation/Ansible/관측 설정 검사 통과 |
| host bootstrap | SSH·UFW·K3s·Traefik 정상, Node `Ready` |
| GitOps bootstrap | root와 필요한 child Application이 `Synced`, `Healthy` |
| secret 공급 | 필요한 SecretStore·ExternalSecret이 `Ready=True` |
| 데이터 계층 | PostgreSQL·role Job·PVC 정상, 외부 restore 연습 완료 |
| edge | DNS-01 인증서 `Ready=True`, Route53 exact A/TXT와 외부 direct HTTPS 검증 |
| 애플리케이션 | 고정 image digest, migration, probe, 핵심 기능 smoke test 통과 |
| 운영 준비 | 외부 uptime 감시, alert 수신, backup RPO/RTO와 복구 절차 검증 |

## 8. 현재 의도적으로 닫힌 gate 찾기

다음은 오류가 아니라 준비되지 않은 상태에서 외부 노출이나 데이터 생성을 막는 안전장치다.

- 앱: 환경별 `deployment.enabled: false`, image가 `bootstrap-required`
- Ingress: 환경별 `ingress.enabled: false`
- DNS: ExternalDNS Zone ID와 공인 IP가 문서용 placeholder
- TLS: 최초 DNS-01 검증을 위한 Let's Encrypt staging issuer
- backup: `suspend: true`
- root: 초기 이관 중 child Application 오삭제를 막는 `prune: false`

이 gate는 한꺼번에 모두 열지 않는다. 관련 작업 가이드의 사전 조건과 성공 기준을 확인한 뒤
필요한 gate만 Git PR로 연다.

## 9. 실행 전에 사람이 확인할 외부 영역

이 저장소의 YAML만으로 다음 항목은 자동 확인되지 않는다.

- 서버의 고정 Public IPv4, 상위 방화벽, 네트워크 CIDR, 영속 디스크와 mount
- GitHub 원격 저장소, 보호된 `main`, CI 성공, public GHCR image와 digest
- AWS CloudFormation stack, Secrets Manager source, ESO bootstrap credential, SES 검증 상태
- Route53 hosted zone과 NS 위임, Hosted Zone ID, 두 controller의 분리된 IAM 자격증명
- Grafana production TLS·public Ingress 준비 상태, 팀원별 Viewer 계정과 Tailscale break-glass 경로
- 운영자가 합의한 backup RPO/RTO와 외부 restore 위치

특히 K3s의 `local-path` PVC는 **실제로 마운트된 디스크 경로**를 사용한다. 현재 임시 Azure의
temporary disk를 영속 DB로 오인하지 말고, 운영 전에는 mount와 재부팅 후 지속성을 별도로 확인한다.

## 10. 안전하게 둘러보는 명령

아래 명령은 외부 시스템을 변경하지 않는다.

```bash
# 현재 수정 중인 파일 확인
git status --short

# 전체 로컬 계약 검사
./scripts/validate.sh

# 환경별 gate와 placeholder 찾기
rg -n 'bootstrap-required|enabled: false|suspend: true' \
  charts argocd manifests

# .env 파일이 Git 추적 대상이 아닌지 확인: 아무것도 출력되지 않아야 함
git ls-files '.env*'

# Ansible이 실행할 대상 확인: 실제 bootstrap은 하지 않음
cd ansible
ansible-inventory --graph
ansible-playbook --syntax-check playbooks/bootstrap.yml
```

`validate.sh`는 로컬에 검사 도구가 없으면 일부 항목을 건너뛸 수 있다. 마지막 한 줄만 보지 말고
skip 메시지가 없는지 확인하며, 첫 bootstrap 전에는 GitHub Actions의 전체 검증 성공도 확인한다.

## 11. 처음 공부할 때의 체크 질문

파일을 읽으며 다음 질문에 답할 수 있으면 충분하다.

1. 이 값은 서버 제공업체, Ansible, Git, AWS Secrets Manager 중 어디가 원본인가?
2. 이 YAML은 Ansible이 직접 적용하는가, Argo CD가 적용하는가?
3. 이 변경은 prod, dev, preview 중 어디까지 영향을 주는가?
4. 실패했을 때 데이터나 접속 경로가 남는가?
5. live 수정이 아니라 Git revert로 되돌릴 수 있는가?
6. `Ready`가 단순 생성 완료인지, 실제 기능 검증까지 뜻하는가?

막힐 때는 파일 하나를 끝까지 해석하려 하지 말고, 먼저 그 파일의 **소유 계층과 다음 소비자**를
찾는다. 예를 들어 `values-prod.yaml`의 값은 Helm이 읽고, 렌더링된 Deployment는 Argo CD가
Kubernetes에 적용하며, 그 Pod가 AWS에서 동기화된 Secret을 소비한다.

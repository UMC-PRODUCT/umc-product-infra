# scripts 운영 도구

이 디렉터리는 애플리케이션 런타임 코드가 아니라 UMC 인프라를 **처음 준비하고, 생성물을
갱신하고, 배포 전에 검증하는 관리자용 도구**를 담는다.

> [!CAUTION]
> `bootstrap_*_access_keys.py`, `bootstrap_aws_secrets.py --apply`,
> `bootstrap-external-secrets-aws.sh`는 외부 상태를
> 변경한다. 각 절의 실행 조건을 확인하지 않고 반복 실행하지 않는다.

## 전체 흐름

```text
CloudFormation stack 배포
  └─ bootstrap_*_access_keys.py
       └─ 로컬 .env.prod / .env.dev / .env.preview 채움
            └─ bootstrap_aws_secrets.py
                 └─ AWS Secrets Manager source 생성
                      └─ Ansible bootstrap
                           └─ ESO secret-zero 주입 → Kubernetes Secret 동기화

observability/ 원본
  └─ gen-configmaps.py
       ├─ manifests/observability/ ConfigMap
       └─ manifests/observability-integrations/ PrometheusRule

모든 변경
  └─ validate.sh
       └─ 로컬 검증 → GitHub Actions 검증 → merge → Argo CD 반영
```

모든 명령은 별도 안내가 없으면 저장소 루트 `umc-infra/`에서 실행한다. 실제 Secret 값은
명령행 인자, Git, 로그나 채팅에 넣지 않는다.

## 한눈에 보기

| 스크립트 | 하는 일 | 변경 범위 | 실행 시점 |
|---|---|---|---|
| [`bootstrap_route53_access_keys.py`](bootstrap_route53_access_keys.py) | cert-manager·ExternalDNS Route53 키 최초 발급 | AWS IAM, `.env.prod` | 최초 1회 |
| [`bootstrap_aws_access_keys.py`](bootstrap_aws_access_keys.py) | prod S3·SES·backup 키 최초 발급 | AWS IAM, `.env.prod` | 최초 1회 |
| [`bootstrap_nonprod_aws_access_keys.py`](bootstrap_nonprod_aws_access_keys.py) | dev/preview S3와 nonprod SES 키 최초 발급 | AWS IAM, `.env.dev`, `.env.preview` | 최초 1회 |
| [`bootstrap_aws_secrets.py`](bootstrap_aws_secrets.py) | worksheet 검증과 33개 Secrets Manager source 생성 | 조회 또는 AWS Secrets Manager | Secret 최초 구성 |
| [`bootstrap-external-secrets-aws.sh`](bootstrap-external-secrets-aws.sh) | ESO가 AWS에 접근할 `aws-bootstrap` Secret 직접 주입 | Kubernetes | 수동 복구·직접 주입 시 |
| [`gen-configmaps.py`](gen-configmaps.py) | 관측 원본을 ConfigMap·PrometheusRule로 변환 | `manifests/observability/`, `manifests/observability-integrations/` | dashboard·alert 변경 시 |
| [`validate.sh`](validate.sh) | 저장소 전체 검증 | 외부 인프라 변경 없음 | 모든 변경 후 |
| `validate_*.py` | 영역별 상세 계약 검사 | 검증 출력 디렉터리 | `validate.sh` 내부 호출 |
| [`argocd_project_kinds.py`](argocd_project_kinds.py) | Application 렌더를 AppProject별로 묶어 필요한 최소 whitelist를 출력하고, 거부될 kind·렌더가 쓰지 않는 whitelist 항목·누락 렌더가 있으면 실패 | `VALIDATION_OUTPUT_DIR` | `validate.sh` 내부 호출, AppProject·chart kind 변경 시 |
| [`tests/`](tests/) | bootstrap·Secret·네트워크 안전장치 단위 테스트 | 없음 | `validate.sh` 내부 호출 |

## 관측 ConfigMap·PrometheusRule 생성

[`gen-configmaps.py`](gen-configmaps.py)는 [`observability/`](../observability/)의 사람이
관리하는 dashboard·Loki alert 원본을 [`manifests/observability/`](../manifests/observability/)의
ConfigMap으로, Prometheus alert 원본을
[`manifests/observability-integrations/`](../manifests/observability-integrations/)의 PrometheusRule로 변환한다.

```bash
# 생성물 갱신
python3 scripts/gen-configmaps.py

# 파일을 바꾸지 않고 drift만 검사
python3 scripts/gen-configmaps.py --check
```

쓰기 모드는 더 이상 허용 목록에 없는 dashboard 생성물을 제거할 수 있다. 원본과 생성물을
같은 commit에서 검토하고, 생성된 ConfigMap·PrometheusRule을 직접 수정하지 않는다.

변경·생성·검증 순서는 [관측 원본 관리](../observability/README.md)를 따른다.

## 저장소 전체 검증

[`validate.sh`](validate.sh)가 사람이 실행하는 단일 검증 진입점이다.

```bash
./scripts/validate.sh
```

주요 검사 범위:

- 관측 원본과 생성된 ConfigMap·PrometheusRule의 drift
- Python 문법과 [`tests/`](tests/) 단위 테스트
- prod/dev/preview 앱 chart와 Secret chart Helm lint·render
- 이미지 tag·digest, 환경값, Secret 이름, namespace와 NetworkPolicy 계약
- PostgreSQL·PostGIS, preview DB lifecycle과 resource budget
- Argo CD, cert-manager, ExternalDNS, Reloader chart와 보안 설정
- kube-prometheus-stack CRD·RBAC·Operator workload, monitor/rule selector와 named Service port
- Grafana, Loki, Tempo, OpenTelemetry 연결과 설정, 원본·렌더된 모든 Prometheus rule 문법
- native AlertmanagerConfig의 global 참조·Discord SecretKeySelector (실제 runtime 설정 생성은 live reconcile에서 확인)
- `argocd/`에 Kubernetes manifest만 있는지와, 앱 chart 입력 변경 시 `Chart.yaml` version 변경 여부
- CloudFormation, Ansible syntax·lint, Kubernetes YAML과 schema
- 남은 과거 이름, Terraform 파일, placeholder와 운영 gate가 의도한 안전 상태인지

Helm, PyYAML과 Git 작업 트리는 필수다. `cfn-lint`, `ansible-playbook`, `ansible-lint`,
`kubeconform`, `promtool`, Docker가 없거나 chart version을 비교할 base ref가 없으면 관련 검사를
건너뛰고, 마지막 줄에 건너뛴 검사를 모두 적는다.

```text
umc-infra validation passed (skipped: CloudFormation lint, Kubernetes schema validation)
```

`VALIDATE_STRICT=1`이면 건너뛴 검사가 하나라도 있을 때 실패한다. CI는 모든 도구를 설치하고
이 모드로 실행한다.

Helm은 `ansible/roles/argocd/defaults/main.yml`의 `argocd_helm_version`과 major·minor가 같아야
한다. 다르면 첫 chart 렌더 전에 필요한 버전을 안내하고 멈춘다. patch만 다르면 경고를 출력하고
계속하며, `VALIDATE_STRICT=1`에서는 patch까지 같아야 한다.

`charts/umc-product-server/`에서 `Chart.yaml` 밖의 파일을 하나라도 바꾸면 같은 변경에서
`Chart.yaml`의 `version`도 바꿔야 한다. 기준은 `VALIDATE_BASE_REF`(기본 `origin/main`)와의 merge
base다. CI는 PR에서 `origin/<base branch>`, `main` push에서 `origin/main`을 넘긴다. `main` push는
merge base가 HEAD라 비교할 변경이 없다. base ref가 없는 clone에서는 이 검사를 건너뛴 검사로 적는다.

Argo CD 검증은 chart checksum을 확인하며 chart 다운로드를 위해 네트워크를 사용한다. 검증 중 Helm
chart 다운로드, Docker image pull과 로컬 cache 변경은 발생할 수 있지만 AWS, GitHub나
Kubernetes 상태는 바꾸지 않는다. 최종 기준은
[GitHub Actions workflow](../.github/workflows/validate.yml)의 `Static validation` 성공이다.

### 내부 validator

아래 파일은 `validate.sh`가 필요한 렌더 결과와 출력 디렉터리를 준비한 뒤 호출한다. 특별한
디버깅 목적이 아니라면 직접 실행하지 않는다.

| 파일 | 검사 영역 |
|---|---|
| [`validate_argocd.py`](validate_argocd.py) | Helm 버전, Argo CD Helm·CRD·workload·ServiceAccount 권한 |
| [`validate_gitops_sources.py`](validate_gitops_sources.py) | root Application이 `bootstrap/root-app.yaml`의 exclude를 빼고 읽는 `argocd/` 파일 구성, 앱 chart version 변경 |
| [`validate_edge_platform.py`](validate_edge_platform.py) | cert-manager·ExternalDNS·Reloader·Route53·TLS/DNS gate |
| [`validate_observability.py`](validate_observability.py) | chart·CRD·RBAC·Operator image/resource·ServiceMonitor·NetworkPolicy·runtime config |
| [`validate_contracts.py`](validate_contracts.py) | AWS·Helm·Secret·PostgreSQL·preview 등 UMC 전용 교차 파일 계약 |

## 단위 테스트

[`tests/`](tests/)는 `validate.sh`에서 자동 실행된다.

| 파일 | 주요 검증 |
|---|---|
| [`test_ansible_bootstrap_contracts.py`](tests/test_ansible_bootstrap_contracts.py) | K3s installer pin, server-side apply, 개인 OpenSSH·UFW와 비공개 Kubernetes API |
| [`test_bootstrap_aws_secrets.py`](tests/test_bootstrap_aws_secrets.py) | worksheet parser, 33개 source mapping, 환경 분리, stdin 전달과 오류 메시지 비노출 |
| [`test_bootstrap_route53_access_keys.py`](tests/test_bootstrap_route53_access_keys.py) | 안전한 `.env.prod`, Route53 stack 계약, access key 발급과 실패 rollback |
| [`test_api_lifecycle_dashboard.py`](tests/test_api_lifecycle_dashboard.py) | API 제거 검토 대시보드의 미관측 표시, 버전 분포와 개인정보 비노출 |
| [`test_email_transport.py`](tests/test_email_transport.py) | 메일 provider 전환 시 발신자·Secret prefix·SMTP egress, Preview Mailpit 격리 |
| [`test_migration_bootstrap.py`](tests/test_migration_bootstrap.py) | 서버 이전용 root Application 단계별 제외 범위와 ExternalDNS 보류 |
| [`test_validate_argocd_access.py`](tests/test_validate_argocd_access.py) | Argo CD 공개 HTTPS Ingress·Certificate·NetworkPolicy와 backend 격리 |
| [`test_validate_contracts.py`](tests/test_validate_contracts.py) | 저장소 디렉터리 구조와 Git에 보이는 파일 판정 |
| [`test_validate_edge_platform.py`](tests/test_validate_edge_platform.py) | 공인 IDC target과 ExternalDNS filter·Grafana target 일치 |
| [`test_validate_observability.py`](tests/test_validate_observability.py) | Operator Pod·monitor selector, named port와 cluster RBAC 권한 경계 |
| [`test_validate_preview_releases.py`](tests/test_validate_preview_releases.py) | Preview 발행 성공 기록 형식과 ApplicationSet의 마지막 성공 image 고정 |
| [`test_validate_skips.py`](tests/test_validate_skips.py) | 건너뛴 검사 요약, `validate.sh` 선택 도구 구간의 모든 skip 기록, `VALIDATE_STRICT=1` 실패 |
| [`test_validate_gitops_sources.py`](tests/test_validate_gitops_sources.py) | `argocd/` 비manifest 파일 거부와 root Application exclude, merge base 기준 chart version 변경 요구 |
| [`test_validate_argocd_helm_version.py`](tests/test_validate_argocd_helm_version.py) | 같은 minor Helm 허용과 strict 모드의 정확한 버전 요구 |

테스트만 실행하려면 다음을 사용한다.

```bash
python3 -m unittest discover -s scripts/tests -p 'test_*.py'
```


## AWS access key 최초 발급

세 스크립트 모두 다음 안전장치를 가진다.

- 로그인한 AWS 계정이 `--expected-account-id`와 같은지 확인한다.
- 서울 region `ap-northeast-2`만 허용한다.
- AWS root 자격증명을 거부한다.
- 필요한 CloudFormation stack과 실제 AWS 자원을 확인한다.
- 대상 IAM 사용자에 기존 access key가 있으면 새 키를 만들지 않는다.
- `.env.*`가 저장소 루트의 일반 파일, mode `0600`, Git ignore 상태인지 확인한다.
- 값을 화면에 출력하지 않고 로컬 worksheet에 기록한다.
- 기록 전 실패하면 이번 실행에서 만든 키를 최대한 자동 삭제한다.

이 스크립트들은 CloudFormation stack이나 IAM 사용자 자체를 만들지 않는다. CloudFormation이
이미 만든 전용 IAM 사용자에 최초 access key만 발급한다. 또한
`umc-external-secrets-bootstrap` 사용자의 key는 이 세 스크립트의 범위가 아니며, 별도로 준비해
Ansible 또는 수동 주입 스크립트에 전달한다.

이 도구들은 **키 회전 도구가 아니다**. 이미 `.env`가 채워졌거나 IAM key가 존재한다면
원인을 확인하고 [Secret 운영 가이드](../docs/operations/secrets.md)의 회전 절차를 사용한다.

### `bootstrap_route53_access_keys.py`

다음 두 IAM 사용자의 키를 각각 발급한다.

- cert-manager: Let's Encrypt DNS-01 검증용 TXT record 관리
- ExternalDNS: 앱 API·Preview 메일 수집함·Grafana·Argo CD DNS record 관리

두 controller가 같은 키를 공유하지 않도록 네 필드를 `.env.prod`에 기록한다.

```bash
python3 scripts/bootstrap_route53_access_keys.py \
  --profile default \
  --expected-account-id "$EXPECTED_AWS_ACCOUNT_ID"
```

선행 조건은 `umc-product-route53-dns` CloudFormation stack 배포다.

### `bootstrap_aws_access_keys.py`

prod 전용 IAM 키 세 묶음을 발급하고 `.env.prod`의 아홉 필드를 채운다.

- 애플리케이션 S3
- prod SES 발송
- PostgreSQL backup S3 writer

```bash
python3 scripts/bootstrap_aws_access_keys.py \
  --profile default \
  --expected-account-id "$EXPECTED_AWS_ACCOUNT_ID"
```

선행 조건은 prod app-storage, SES, backup CloudFormation stack 배포다.

### `bootstrap_nonprod_aws_access_keys.py`

다음을 발급하고 `.env.dev`, `.env.preview`를 함께 갱신한다.

- dev 전용 S3 key
- preview 전용 S3 key
- dev/preview가 공유하는 nonprod SES key

prod SES key는 공유하지 않는다.

```bash
python3 scripts/bootstrap_nonprod_aws_access_keys.py \
  --profile default \
  --expected-account-id "$EXPECTED_AWS_ACCOUNT_ID"
```

선행 조건은 dev·preview app-storage와 SES CloudFormation stack 배포다.

## AWS Secrets Manager source 생성

[`bootstrap_aws_secrets.py`](bootstrap_aws_secrets.py)는 세 worksheet를 읽어 Helm의
ExternalSecret 계약과 일치하는 33개 JSON source를 구성한다. 이 중 prod/dev/preview의
`docs-basic-auth` source는 Scalar/OpenAPI 문서용 bcrypt htpasswd 값을, prod의
`postgres-exporter` source는 모니터링 전용 DB role password를 공급한다.

기본 실행은 AWS를 변경하지 않는 **dry-run**이다.

```bash
python3 scripts/bootstrap_aws_secrets.py \
  --profile default \
  --expected-account-id "$EXPECTED_AWS_ACCOUNT_ID"
```

상태의 의미는 다음과 같다.

| 상태 | 의미 |
|---|---|
| `WOULD_CREATE` | AWS에 없으며 `--apply` 시 생성됨 |
| `CREATE` | `--apply` 실행에서 생성할 대상으로 판정됨 |
| `CREATED` | 해당 source 생성과 생성 후 값 검증이 끝남 |
| `SKIP_MATCHED` | AWS 값과 worksheet가 이미 일치함 |
| `CONFLICT` | 기존 값이 다르거나 삭제 대기 중이라 중단됨 |

dry-run 결과를 검토한 뒤 누락된 source만 생성한다.

```bash
python3 scripts/bootstrap_aws_secrets.py \
  --profile default \
  --expected-account-id "$EXPECTED_AWS_ACCOUNT_ID" \
  --apply
```

`--apply`도 기존 source를 업데이트하거나 삭제하지 않는다. Firebase, OAuth, DB 비밀번호처럼
기존 값을 바꾸는 작업은 명시적인 Secret 회전 절차로 수행한다. 여러 source를 생성하던 중
실패하면 앞에서 성공한 source는 삭제하지 않는다. 원인을 해결하고 다시 실행하면 이미 생성된
source는 `SKIP_MATCHED`로 건너뛴다.

## ESO bootstrap Secret 직접 주입

[`bootstrap-external-secrets-aws.sh`](bootstrap-external-secrets-aws.sh)는 AWS 밖의 K3s에서
External Secrets Operator가 최초로 AWS IAM role을 사용할 수 있도록 다음 Secret을 적용한다.

```text
namespace: external-secrets
name: aws-bootstrap
```

값은 환경변수로만 받고 Kubernetes API에 stdin으로 전달한다. ESO가 이미 실행 중이면 새 키를
읽도록 controller를 재시작하고 rollout을 기다린다.

승인된 비밀 저장소가 `ESO_AWS_ACCESS_KEY_ID`, `ESO_AWS_SECRET_ACCESS_KEY` 두 환경변수를
프로세스에 비표시 방식으로 주입한 shell에서 실행한다. 실제 값을 `export` 명령에 직접 적으면
shell history에 남을 수 있다.

```bash
scripts/bootstrap-external-secrets-aws.sh
```

표준 설치에서는 [Ansible bootstrap](../ansible/README.md)의
`external_secrets_bootstrap` role이 같은 역할을 담당한다. 일반적인 최초 배포나 회전에서는
Ansible을 사용하고, 이 스크립트는 수동 복구나 직접 `kubectl`로 관리할 때만 사용한다.

## 최초 설치와 운영 절차

AWS 자원과 Secret 준비 순서는 [Secret 운영](../docs/operations/secrets.md),
서버 설치는 [Ansible 초기 구성](../docs/operations/ansible-bootstrap.md)을 따른다.
이 페이지는 각 도구의 입력·출력과 변경 범위를 설명하며 별도의 설치 순서를 복제하지 않는다.

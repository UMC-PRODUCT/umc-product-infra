# Secret 운영

Secret 원본은 AWS Secrets Manager다. External Secrets Operator(ESO)가 namespace별
Kubernetes Secret으로 동기화한다. Git에는 값이 아니라 경로, property 이름과 IAM 정책만 둔다.

```text
로컬 .env.* worksheet
  → bootstrap script
  → AWS Secrets Manager
  → ESO SecretStore / ExternalSecret
  → Kubernetes Secret
  → Pod
```

## 절대 규칙

- Secret 값, access key, API token, private key와 PEM을 Git, issue, chat, 명령 인자나 로그에 넣지 않는다.
- `kubectl get secret -o yaml/json`과 base64 decode 결과를 공유하지 않는다.
- `.env.prod`, `.env.dev`, `.env.preview`는 Git에서 제외하고 소유자만 읽도록 mode `0600`을 유지한다.
- ESO bootstrap key는 승인된 password manager에만 보관한다. 사람의 AWS CLI profile로 사용하지 않는다.
- DB 관리자 Secret은 앱 namespace에 복제하지 않는다.
- GHCR은 public anonymous pull이므로 image pull Secret은 만들지 않는다.

## 어디를 수정하나

| 목적 | 기준 파일 |
|---|---|
| Secret 경로·property·대상 namespace | `charts/umc-secrets/values.yaml` |
| SecretStore·ExternalSecret 생성 방식 | `charts/umc-secrets/templates/` |
| 로컬 worksheet 검증과 AWS source 생성 | `scripts/bootstrap_aws_secrets.py` |
| namespace별 AWS 읽기 권한 | `cloud/aws/external-secrets-iam.yaml` |
| 앱·SES·backup·DNS access key 최초 발급 | `scripts/bootstrap_*_access_keys.py` |
| Pod가 읽는 Secret 이름 | `charts/umc-product-server/templates/deployment.yaml` |
| API 문서 Basic Auth 적용 | `charts/umc-product-server/templates/documentation-ingress.yaml` |

환경별 AWS 경로는 `/umc-product/prod/`, `/umc-product/dev/`,
`/umc-product/preview/`, `/umc-product/platform/`으로 분리한다. 정확한 source 개수와
property 목록은 문서에 복사하지 않고 위 코드에서 확인한다.

## 값 형식 주의

- `APPLE_PRIVATE_KEY`는 PEM 개행을 보존한다.
- `FIREBASE_CONFIGURATION`은 service-account JSON 전체를 문자열로 저장하며 base64로 바꾸지 않는다.
- `DOCS_BASIC_AUTH_USERS`는 `umc-docs:<bcrypt hash>` 형식의 htpasswd 한 줄이다. 원문
  비밀번호는 로컬 worksheet에서만 확인하고 AWS·Kubernetes에는 이 해시만 저장한다.
- prod SES 자격증명은 dev/preview와 공유하지 않는다. dev/preview만 nonprod sender를 공유한다.
- DB URL·username, bucket 이름, region, Spring profile, issuer URL과 발신 주소는 비밀이 아니므로 Helm values에 둔다.

## 최초 준비

모든 명령은 저장소 루트의 private terminal에서 실행한다.

1. `aws sts get-caller-identity`로 계정과 `ap-northeast-2` region을 확인한다.
2. `cloud/aws/`의 CloudFormation을 배포한다.
3. 전용 스크립트로 최초 access key를 `.env.*`에 기록한다.
4. `bootstrap_aws_secrets.py` dry-run을 검토한 뒤 missing source만 생성한다.
5. ESO bootstrap IAM key를 password manager에 보관한다.
6. Ansible bootstrap에서 ESO key를 비표시 입력으로 주입한다.
7. 모든 SecretStore와 ExternalSecret이 `Ready=True`인지 확인한다.

CloudFormation 순서는 다음과 같다.

| 순서 | Template | 배포 단위 |
|---:|---|---|
| 1 | `cloud/aws/route53-dns.yaml` | 기존 Hosted Zone용 controller IAM |
| 2 | `cloud/aws/external-secrets-iam.yaml` | ESO bootstrap user와 namespace별 role |
| 3 | `cloud/aws/app-storage-s3.yaml` | prod/dev/preview 각각 한 번 |
| 4 | `cloud/aws/ses-email.yaml` | 공유 domain identity와 prod/nonprod sender |
| 5 | `cloud/aws/postgres-backup-s3.yaml` | prod backup bucket과 write-only user |

CloudFormation은 access key와 Secrets Manager 값을 만들지 않는다. Template의 `Parameters`와
`Outputs`가 배포 계약이며, 실제 값은 콘솔·CLI 결과로 검증한다.

```bash
aws sts get-caller-identity --profile default

python3 scripts/bootstrap_route53_access_keys.py \
  --profile default \
  --expected-account-id "$EXPECTED_AWS_ACCOUNT_ID"

python3 scripts/bootstrap_aws_access_keys.py \
  --profile default \
  --expected-account-id "$EXPECTED_AWS_ACCOUNT_ID"

python3 scripts/bootstrap_nonprod_aws_access_keys.py \
  --profile default \
  --expected-account-id "$EXPECTED_AWS_ACCOUNT_ID"

# read-only dry-run
python3 scripts/bootstrap_aws_secrets.py \
  --profile default \
  --expected-account-id "$EXPECTED_AWS_ACCOUNT_ID"

# 검토 후 missing source 생성
python3 scripts/bootstrap_aws_secrets.py \
  --profile default \
  --expected-account-id "$EXPECTED_AWS_ACCOUNT_ID" \
  --apply
```

초기 발급 스크립트는 기존 IAM key나 채워진 worksheet를 발견하면 중단한다. 이를 회전 도구로
사용하지 않는다. `bootstrap_aws_secrets.py`도 기존 값이 다르면 자동 overwrite하지 않는다.

## 동기화 확인

클러스터 명령은 IDC 서버에서 실행한다. Secret 값은 조회하지 않는다.

```bash
sudo k3s kubectl get secretstore -A
sudo k3s kubectl get externalsecret -A
sudo k3s kubectl wait --for=condition=Ready secretstore --all -A --timeout=180s
sudo k3s kubectl wait --for=condition=Ready externalsecret --all -A --timeout=300s
```

AWS 값을 즉시 반영해야 할 때만 해당 ExternalSecret을 force-sync한다.

```bash
sudo k3s kubectl annotate externalsecret "$NAME" -n "$NAMESPACE" \
  external-secrets.io/force-sync="$(date +%s)" --overwrite
sudo k3s kubectl wait --for=condition=Ready "externalsecret/$NAME" \
  -n "$NAMESPACE" --timeout=180s
```

Reloader는 허용된 앱 Secret 변경만 Pod 재시작으로 연결한다. DB와 JWT는 자동 재시작 대상으로
간주하지 않는다. 실제 허용 목록은 `charts/umc-secrets/values.yaml`이 기준이다.

문서 인증 Secret은 Pod가 아니라 Traefik Middleware가 직접 읽는다. ESO 동기화가 끝나면
Traefik이 변경을 감지하므로 애플리케이션 Pod를 재시작하지 않는다. 비밀번호를 바꿀 때는
비밀번호를 명령 인자에 넣지 말고 아래처럼 대화형 입력으로 새 bcrypt htpasswd 값을 만든다.

```bash
htpasswd -nB -C 12 umc-docs
```

환경별 `/umc-product/prod/docs-basic-auth` 또는
`/umc-product/dev/docs-basic-auth`의 `users` property를 갱신하고 해당
`docs-basic-auth` ExternalSecret을 force-sync한 뒤, 익명 요청은 `401`, 새 자격증명 요청은
`200`인지 확인한다.

## 앱 DB 비밀번호 회전

순서를 바꾸면 기존 Pod와 DB role의 비밀번호가 어긋난다.

1. 환경의 `app-db` AWS source에 새 `DATABASE_PASSWORD` version을 만든다.
2. 앱 namespace와 DB namespace의 `app-db` ExternalSecret을 모두 force-sync한다.
3. 두 리소스가 `Ready=True`이고 refresh time이 바뀌었는지 확인한다.
4. 환경의 `postgres-app-role` Job을 다시 실행해 실제 DB role에 새 비밀번호를 적용한다.
5. 그다음 앱 Deployment를 재시작한다.
6. readiness, DB query와 telemetry를 확인한 뒤 이전 AWS secret version을 폐기한다.

단일 replica이므로 재시작 중 짧은 중단이 생길 수 있다.

## 회전 공통 원칙

모든 회전은 **새 credential 생성 → AWS source 갱신 → ESO Ready 확인 → 소비자 재시작 또는 Job
재실행 → smoke test → 이전 credential 폐기** 순서다.

- JWT key는 기존 token을 무효화할 수 있으므로 cutover와 rollback을 먼저 정한다.
- Route 53 controller 두 개는 서로 다른 key를 사용한다. 상세 순서는 [도메인/TLS 가이드](domains-tls.md#route-53-access-key-회전)를 따른다.
- backup writer는 새 key로 수동 backup과 외부 restore를 검증하기 전 이전 key를 폐기하지 않는다.
- Grafana admin Secret은 빈 PVC 최초 bootstrap용이며 UI에서 바꾼 비밀번호와 자동 동기화되지 않는다.

ExternalSecret은 remote source 장애 시 마지막 정상 Secret을 보존한다. 그러나 Git에서
ExternalSecret 자체를 제거하면 target Secret도 함께 삭제될 수 있으므로 소비자를 먼저 제거한다.

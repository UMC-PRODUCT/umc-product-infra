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
- `POSTGRES_EXPORTER_PASSWORD`는 prod 모니터링 role 전용 값이다. DB 관리자,
  앱, readonly password와 모두 다른 난수를 쓴다.
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

## 임시 Gmail SMTP와 SES 복귀

백엔드의 `EMAIL_PROVIDER=ses|smtp` 지원 이미지가 필요하다. 기존 SES 전용 이미지에
Gmail 발신 주소만 넣으면 SMTP로 바뀌지 않는다. 인증번호·HTML 템플릿은 유지하고 발송 경로만
바꾸며, 실패 시 다른 provider로 자동 재발송하지 않는다.

메일 provider 전환은 `values-dev.yaml`에서 실제 발송을 검증한 뒤 `values-prod.yaml`에 적용한다.
각 환경의 지원 이미지와 provider·발신자 설정을 일치시키고, prod 전환은 별도 Git 변경으로 진행한다.

- SMTP 환경은 `smtp.gmail.com:587`에 STARTTLS·인증·서버 인증서 검증을 적용한다.
  `SMTP_USERNAME`과 `EMAIL_NO_REPLY_ADDRESS`는 `umcproduct1227@gmail.com`으로 동일하게 둔다.
- Gmail 계정의 2단계 인증을 활성화하고 발급한 **앱 비밀번호**를 공백 없이
  `.env.prod`와 `.env.dev`의 `SMTP_PASSWORD`에 입력한다. 일반 로그인 비밀번호는 사용하지 않는다.
- `/umc-product/prod/app-email`, `/umc-product/dev/app-email`의 기존 JSON에
  `SMTP_PASSWORD` property만 추가한다. SES 키와 다른 property를 삭제·덮어쓰지 않는다.
  `bootstrap_aws_secrets.py --apply`는 기존 Secret 갱신 도구가 아니므로 AWS 콘솔의
  **보안 암호 값 검색 → 편집**에서 갱신하거나, 기존 JSON을 보존하는 안전한 갱신 절차를 사용한다.
- preview는 SES만 사용한다. Gmail 앱 비밀번호를 preview Secret에 복제하지 않는다.

적용 순서:

1. 백엔드 SMTP 지원 코드를 develop에 반영해 빌드·검증하고 GHCR image tag/digest를 확보한다.
   infra PR의 dev image pin도 해당 지원 이미지로 갱신하기 전에는 merge하지 않는다.
2. **prod와 dev 양쪽** AWS `app-email` source에 앱 비밀번호를 먼저 추가한다.
   ESO 매핑은 양쪽에 추가되므로, prod가 아직 SES를 사용해도 두 source 모두 준비해야 한다.
3. `umc-secrets` 매핑을 반영하고 app/dev-app의 `app-email` ExternalSecret `Ready=True`와
   refresh time 갱신을 확인한다. 기존 prod도 Reloader로 재시작될 수 있으므로 확인한다.
4. dev의 **검증한 새 이미지 + EMAIL_PROVIDER=smtp + Gmail 발신자**를 함께 반영한다.
   rollout/readiness뿐 아니라 인증메일 요청 → Gmail/네이버 수신 → 코드 검증까지 확인한다.
5. dev 검증 후 백엔드 main에도 SMTP 지원 코드를 반영하고 prod 지원 이미지 tag/digest를
   확보한다. 그다음 prod values의 이미지와 SMTP 환경변수를 함께 변경하고 동일하게 검증한다.
   SMTP 서버가 메일을 접수한 것과 받은편지함에 도착한 것은 별개다.

NetworkPolicy는 SMTP 모드일 때만 public IPv4의 TCP/587 **egress**를 추가한다.
SMTP inbound, NodePort, public SSH는 열지 않는다. 표준 NetworkPolicy는 FQDN을 지원하지
않으므로 Gmail IP만으로 제한하는 정책은 아니며, 기존 사설망·metadata 주소 제외를 유지한다.

개인 Gmail은 하루 500통 초과 시 제한될 수 있으며 500통의 수신 성공을 보장하는 서비스도 아니다.
같은 Gmail 계정의 dev·prod·수동 발송과 재발송이 한도를 공유하므로 **700명 전체 발송 대책으로는
부족하다**. SMTP 발송은 SES를 거치지 않으므로 SES의 SNS 알림·suppression도 적용되지 않는다.
[Google 발송 한도](https://support.google.com/mail/answer/22839?hl=en),
[앱 비밀번호](https://support.google.com/accounts/answer/185833?hl=en)를 참고한다.

SES 승인 후에는 prod의 `EMAIL_NO_REPLY_ADDRESS=no-reply@university.neordinary.com`,
dev의 `EMAIL_NO_REPLY_ADDRESS=no-reply-nonprod@university.neordinary.com`과 함께
`EMAIL_PROVIDER=ses`로 변경한다. SES 키·region·configuration set은 유지한다.
GitOps rollout 후 실제 수신을 확인하면 SMTP egress도 다시 닫힌다. 두 환경이 모두 SES로
돌아온 뒤 Gmail 앱 비밀번호를 폐기하고, ESO 매핑·bootstrap property 정의에서 제거한 다음
AWS source와 로컬 worksheet에서도 지운다. ESO 매핑이 남아 있을 때 property부터 지우면
`app-email` 동기화가 실패할 수 있다.

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

## PostgreSQL exporter password 회전

1. `/umc-product/prod/postgres-exporter`의 `POSTGRES_EXPORTER_PASSWORD`를 새 값으로 갱신한다.
2. `db/postgres-exporter` ExternalSecret을 force-sync하고 `Ready=True`를 확인한다.
3. `postgres-exporter-role` Job을 재실행해 DB role password를 먼저 바꾼다.
4. `postgres-exporter` Deployment를 재시작한다.
5. Prometheus에서 `up{job="postgres-exporter"} == 1`을 확인한 뒤 이전 version을 폐기한다.

이 절차는 별도 exporter Deployment만 재시작하며 PostgreSQL StatefulSet을 재시작하지 않는다.

## 회전 공통 원칙

모든 회전은 **새 credential 생성 → AWS source 갱신 → ESO Ready 확인 → 소비자 재시작 또는 Job
재실행 → smoke test → 이전 credential 폐기** 순서다.

- JWT key는 기존 token을 무효화할 수 있으므로 cutover와 rollback을 먼저 정한다.
- Route 53 controller 두 개는 서로 다른 key를 사용한다. 상세 순서는 [도메인/TLS 가이드](domains-tls.md#route-53-access-key-회전)를 따른다.
- backup writer는 새 key로 수동 backup과 외부 restore를 검증하기 전 이전 key를 폐기하지 않는다.
- Grafana admin Secret은 빈 PVC 최초 bootstrap용이며 UI에서 바꾼 비밀번호와 자동 동기화되지 않는다.

ExternalSecret은 remote source 장애 시 마지막 정상 Secret을 보존한다. 그러나 Git에서
ExternalSecret 자체를 제거하면 target Secret도 함께 삭제될 수 있으므로 소비자를 먼저 제거한다.

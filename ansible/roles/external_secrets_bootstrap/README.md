# `external_secrets_bootstrap` 역할

External Secrets Operator(ESO)가 AWS Secrets Manager에 접근할 수 있도록 Kubernetes의 secret-zero를 준비한다.
발급은 최초 한 번이지만 ESO가 런타임에 계속 사용하는 장기 자격증명이므로 일회용 bootstrap token은 아니다.

## 하는 일

1. AWS access key를 역할 변수, 제어 노드 환경변수, 비표시 입력 순서로 가져온다.
2. `external-secrets` namespace를 만든다.
3. 자격증명을 `aws-bootstrap` Kubernetes Secret으로 server-side apply한다.
4. 기존 ESO가 있는 상태에서 자격증명이 바뀌면 controller를 재시작하고 rollout을 확인한다.
5. 작업이 끝나면 후속 task에서 사용하지 않도록 Ansible fact의 값을 비운다.

이 역할은 ESO 자체를 설치하지 않는다. secret-zero가 준비된 뒤 Argo CD root Application이 ESO를 설치하고 이후 플랫폼 리소스를 관리한다.

## 자격증명 입력 순서

1. `external_secrets_bootstrap_aws_access_key_id` / `external_secrets_bootstrap_aws_secret_access_key`
2. 제어 노드 환경변수 `ESO_AWS_ACCESS_KEY_ID` / `ESO_AWS_SECRET_ACCESS_KEY`
3. playbook 실행 중 비표시 prompt

## 주요 변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `external_secrets_bootstrap_namespace` | `external-secrets` | 저장소 전체에 고정된 namespace |
| `external_secrets_bootstrap_secret_name` | `aws-bootstrap` | 저장소 전체에 고정된 Secret 이름 |
| `external_secrets_bootstrap_aws_access_key_id` | 빈 문자열 | AWS access key ID |
| `external_secrets_bootstrap_aws_secret_access_key` | 빈 문자열 | AWS secret access key |

## 파일 구성

| 경로 | 역할 |
|---|---|
| `defaults/main.yml` | namespace, Secret 이름, 자격증명 입력 변수 |
| `tasks/main.yml` | 자격증명 확인과 Secret 적용 |
| `handlers/main.yml` | 자격증명 회전 시 기존 ESO 재시작 |

## 주의사항

- 실제 자격증명을 inventory, Git, 명령행 `--extra-vars`에 저장하지 않는다. 환경변수 또는 prompt 사용을 권장한다.
- namespace와 Secret 이름은 ESO Helm values에도 고정돼 있으므로 이 역할에서만 override할 수 없다.
- 민감 작업은 `no_log`이며 Secret manifest는 임시 파일이나 프로세스 인자 대신 stdin으로 전달된다.
- 마지막에 fact를 비우는 것은 후속 노출을 줄일 뿐 프로세스 메모리의 안전한 삭제를 보장하지 않는다.
- Kubernetes Secret의 저장 시 암호화가 먼저 활성화되어 있어야 하므로 `k3s` 역할 뒤에 실행한다.

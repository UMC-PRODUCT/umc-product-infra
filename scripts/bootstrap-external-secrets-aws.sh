#!/usr/bin/env bash
# AWS 밖의 k3s가 Secrets Manager 역할을 AssumeRole할 bootstrap IAM 자격증명 하나만 주입한다.
# 값은 Git·프로세스 인자·로그에 남기지 않고 환경변수와 stdin으로만 전달한다.
set -euo pipefail
set +x

: "${ESO_AWS_ACCESS_KEY_ID:?ESO_AWS_ACCESS_KEY_ID가 필요하다}"
: "${ESO_AWS_SECRET_ACCESS_KEY:?ESO_AWS_SECRET_ACCESS_KEY가 필요하다}"

kubectl create namespace external-secrets --dry-run=client -o yaml | kubectl apply -f -

access_key_id_b64="$(printf '%s' "${ESO_AWS_ACCESS_KEY_ID}" | base64 | tr -d '\n')"
secret_access_key_b64="$(printf '%s' "${ESO_AWS_SECRET_ACCESS_KEY}" | base64 | tr -d '\n')"

kubectl apply --server-side --force-conflicts --field-manager=umc-bootstrap -f - <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: aws-bootstrap
  namespace: external-secrets
  labels:
    app.kubernetes.io/managed-by: operator-bootstrap
    infra.university.neordinary.com/secret-purpose: external-secrets-aws-auth
type: Opaque
data:
  access-key-id: ${access_key_id_b64}
  secret-access-key: ${secret_access_key_b64}
EOF

unset access_key_id_b64 secret_access_key_b64
unset ESO_AWS_ACCESS_KEY_ID ESO_AWS_SECRET_ACCESS_KEY

# 이미 ESO가 떠 있는 클러스터에서 key를 회전한 경우 새 환경변수를 읽게 한다.
if kubectl get deployment/external-secrets -n external-secrets >/dev/null 2>&1; then
  kubectl rollout restart deployment/external-secrets -n external-secrets >/dev/null
  kubectl rollout status deployment/external-secrets \
    -n external-secrets --timeout=300s
fi

echo "external-secrets/aws-bootstrap 주입 완료 (값은 출력하지 않음)"

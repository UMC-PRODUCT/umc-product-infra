#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
cd "$repo_root"

command -v helm >/dev/null || {
  echo "helm is required" >&2
  exit 1
}
python3 -c 'import yaml' >/dev/null 2>&1 || {
  echo "PyYAML is required" >&2
  exit 1
}

if [[ -n "${VALIDATION_OUTPUT_DIR:-}" ]]; then
  validation_dir="$VALIDATION_OUTPUT_DIR"
  mkdir -p "$validation_dir"
else
  validation_dir="$(mktemp -d -t umc-infra-validation.XXXXXX)"
  trap 'rm -rf -- "$validation_dir"' EXIT
fi

readonly validation_tag=0123456789ab
readonly validation_digest=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
# RFC 5737 TEST-NET-3. 합성 Ingress 렌더 검증에만 쓰며 운영 target으로 허용하지 않는다.
readonly test_net_edge_target=203.0.113.10
common_args=(
  --set deployment.enabled=true
  --set-string "image.tag=$validation_tag"
  --set-string "image.digest=$validation_digest"
  --set-string env.DEMODAY_QR_BASE_URL=https://validation.example.com
)

python3 scripts/gen-configmaps.py --check
PYTHONPYCACHEPREFIX="$validation_dir/pycache" python3 -m py_compile scripts/*.py
PYTHONPYCACHEPREFIX="$validation_dir/pycache" \
  python3 -m unittest discover -s scripts/tests -p 'test_*.py'

# Base는 안전하게 빈 workload를 렌더하고, 각 환경은 gate를 연 상태까지 검증한다.
helm lint charts/umc-product-server --strict

# Git에 선언된 실제 values 조합이 Argo CD에서도 그대로 렌더되는지 확인한다.
# 합성 검증의 --set override가 누락된 운영값을 가리지 않게 한다.
helm template umc-product-server charts/umc-product-server \
  --namespace app \
  -f charts/umc-product-server/values-prod.yaml \
  >"$validation_dir/prod-desired-state.yaml"
helm template dev-umc-product-server charts/umc-product-server \
  --namespace dev-app \
  -f charts/umc-product-server/values-dev.yaml \
  >"$validation_dir/dev-desired-state.yaml"
helm template umc-product-preview charts/umc-product-server \
  --namespace preview \
  -f charts/umc-product-server/values-preview.yaml \
  >"$validation_dir/preview-desired-state.yaml"

# 실제 FE origin을 넣지 않으면 workload gate가 열리지 않아야 한다.
if helm template umc-product-server charts/umc-product-server \
  -f charts/umc-product-server/values-prod.yaml "${common_args[@]}" \
  --set-string env.DEMODAY_QR_BASE_URL=https://bootstrap-required.example.invalid \
  >/dev/null 2>&1; then
  echo "DEMODAY_QR_BASE_URL bootstrap gate unexpectedly passed" >&2
  exit 1
fi

# prod는 FCM을 끌 수 없고, FCM을 켠 Pod에는 Firebase Secret이 반드시 필요하다.
if helm template umc-product-server charts/umc-product-server \
  -f charts/umc-product-server/values-prod.yaml "${common_args[@]}" \
  --set-string env.FCM_ENABLED=false >/dev/null 2>&1; then
  echo "prod FCM_ENABLED=false unexpectedly passed" >&2
  exit 1
fi
if helm template umc-product-server charts/umc-product-server \
  -f charts/umc-product-server/values-prod.yaml "${common_args[@]}" \
  --set-string env.FCM_ENABLED=true \
  --set 'requiredSecrets={app-db,app-jwt,app-oauth,app-storage,app-email}' \
  >/dev/null 2>&1; then
  echo "FCM without app-fcm unexpectedly passed" >&2
  exit 1
fi
if helm template dev-umc-product-server charts/umc-product-server \
  -f charts/umc-product-server/values-dev.yaml "${common_args[@]}" \
  --set-string env.FCM_ENABLED=true >/dev/null 2>&1; then
  echo "dev FCM_ENABLED=true unexpectedly passed" >&2
  exit 1
fi

# 환경별 S3 bucket을 섞으면 데이터와 삭제 권한의 경계가 무너진다.
if helm template umc-product-server charts/umc-product-server \
  -f charts/umc-product-server/values-prod.yaml "${common_args[@]}" \
  --set-string env.S3_BUCKET_NAME=umc-product-dev-app-storage-351284652562-ap-northeast-2 \
  >/dev/null 2>&1; then
  echo "prod dev S3 bucket unexpectedly passed" >&2
  exit 1
fi
if helm template dev-umc-product-server charts/umc-product-server \
  -f charts/umc-product-server/values-dev.yaml "${common_args[@]}" \
  --set-string env.S3_BUCKET_NAME=umc-product-preview-app-storage-351284652562-ap-northeast-2 \
  >/dev/null 2>&1; then
  echo "dev preview S3 bucket unexpectedly passed" >&2
  exit 1
fi
if helm template dev-umc-product-server charts/umc-product-server \
  -f charts/umc-product-server/values-dev.yaml "${common_args[@]}" \
  --set-string env.S3_REGION=us-east-1 >/dev/null 2>&1; then
  echo "non-Seoul S3 region unexpectedly passed" >&2
  exit 1
fi

# prod와 nonprod 발신 주소가 뒤섞이면 IAM FromAddress 조건과 실제 runtime이 어긋난다.
if helm template umc-product-server charts/umc-product-server \
  -f charts/umc-product-server/values-prod.yaml "${common_args[@]}" \
  --set-string env.EMAIL_NO_REPLY_ADDRESS=no-reply-nonprod@university.neordinary.com \
  >/dev/null 2>&1; then
  echo "prod nonprod sender address unexpectedly passed" >&2
  exit 1
fi
if helm template dev-umc-product-server charts/umc-product-server \
  -f charts/umc-product-server/values-dev.yaml "${common_args[@]}" \
  --set-string env.EMAIL_NO_REPLY_ADDRESS=no-reply@university.neordinary.com \
  >/dev/null 2>&1; then
  echo "dev production sender address unexpectedly passed" >&2
  exit 1
fi

# Ingress가 닫힌 bootstrap 상태에서는 staging issuer와 차단 target을 계속 허용한다.
helm template umc-product-server charts/umc-product-server \
  -f charts/umc-product-server/values-prod.yaml "${common_args[@]}" \
  --set ingress.enabled=false \
  --set-string certificate.issuerRef.name=letsencrypt-staging \
  --set-string externalDNS.target=bootstrap-required \
  >/dev/null

# 외부 Ingress는 production Certificate와 실제 target 계약을 동시에 만족해야 한다.
# TEST-NET은 이 negative render에서 형식 검증용으로만 사용한다.
if helm template umc-product-server charts/umc-product-server \
  -f charts/umc-product-server/values-prod.yaml "${common_args[@]}" \
  --set ingress.enabled=true \
  --set-string certificate.issuerRef.name=letsencrypt-staging \
  --set-string "externalDNS.target=$test_net_edge_target" \
  >/dev/null 2>&1; then
  echo "Ingress with staging Certificate unexpectedly passed" >&2
  exit 1
fi
if helm template umc-product-server charts/umc-product-server \
  -f charts/umc-product-server/values-prod.yaml "${common_args[@]}" \
  --set ingress.enabled=true \
  --set-string certificate.issuerRef.name=letsencrypt-production \
  --set-string externalDNS.target=bootstrap-required \
  >/dev/null 2>&1; then
  echo "Ingress with bootstrap ExternalDNS target unexpectedly passed" >&2
  exit 1
fi

helm lint charts/umc-product-server --strict \
  -f charts/umc-product-server/values-prod.yaml "${common_args[@]}"
helm template umc-product-server charts/umc-product-server \
  --namespace app \
  -f charts/umc-product-server/values-prod.yaml "${common_args[@]}" \
  >"$validation_dir/prod.yaml"

helm lint charts/umc-product-server --strict \
  -f charts/umc-product-server/values-dev.yaml "${common_args[@]}"
helm template dev-umc-product-server charts/umc-product-server \
  --namespace dev-app \
  -f charts/umc-product-server/values-dev.yaml "${common_args[@]}" \
  >"$validation_dir/dev.yaml"

preview_args=(
  "${common_args[@]}"
  --set-string image.digest=
  --set-string fullnameOverride=umc-product-server-pr-42
  --set-string createDatabase.name=umc_product_pr42
  --set-string env.DATABASE_URL=jdbc:postgresql://postgres-preview.preview.svc.cluster.local:5432/umc_product_pr42
  --set-string env.SPRING_APPLICATION_NAME=preview-umc-product-pr42
  --set-string env.APP_ENVIRONMENT=preview-pr-42
  --set-string env.SSO_ISSUER=https://api-pr-42.university.neordinary.com
  --set-string 'env.CERTIFICATE_VERIFICATION_URL_TEMPLATE=https://api-pr-42.university.neordinary.com/api/v1/certificates/verify/{serialNumber}'
  --set-string ingress.host=api-pr-42.university.neordinary.com
  --set-string 'ingress.tls[0].hosts[0]=api-pr-42.university.neordinary.com'
)
if helm template umc-product-preview-42 charts/umc-product-server \
  -f charts/umc-product-server/values-preview.yaml "${preview_args[@]}" \
  --set-string env.S3_BUCKET_NAME=umc-product-prod-app-storage-351284652562-ap-northeast-2 \
  >/dev/null 2>&1; then
  echo "preview prod S3 bucket unexpectedly passed" >&2
  exit 1
fi
helm lint charts/umc-product-server --strict \
  -f charts/umc-product-server/values-preview.yaml "${preview_args[@]}"
helm template umc-product-preview-42 charts/umc-product-server \
  --namespace preview \
  -f charts/umc-product-server/values-preview.yaml "${preview_args[@]}" \
  >"$validation_dir/preview.yaml"

# 운영값과 분리된 TEST-NET 합성 렌더로 production TLS/DNS/Ingress 계약을 검증한다.
helm template umc-product-server charts/umc-product-server \
  --namespace app \
  -f charts/umc-product-server/values-prod.yaml "${common_args[@]}" \
  --set ingress.enabled=true \
  --set-string certificate.issuerRef.name=letsencrypt-production \
  --set-string "externalDNS.target=$test_net_edge_target" \
  >"$validation_dir/active-edge-test-net.yaml"

# Preview는 PR별 Certificate를 만들지 않고 공용 wildcard TLS Secret을 참조한다.
helm template umc-product-preview-42 charts/umc-product-server \
  --namespace preview \
  -f charts/umc-product-server/values-preview.yaml "${preview_args[@]}" \
  --set ingress.enabled=true \
  --set-string "externalDNS.target=$test_net_edge_target" \
  >"$validation_dir/active-preview-edge-test-net.yaml"

secrets_args=(--set-string aws.accountId=123456789012)
helm lint charts/umc-secrets --strict "${secrets_args[@]}"
helm template umc-secrets charts/umc-secrets \
  --namespace external-secrets "${secrets_args[@]}" \
  >"$validation_dir/secrets.yaml"

# bootstrap chart의 checksum과 appVersion을 확인하고 실제 values 결과를 검사한다.
python3 scripts/validate_argocd.py "$validation_dir"

# Argo CD에 인라인으로 둔 valuesObject를 그대로 사용해 외부 관측 chart도 렌더한다.
# chart 기본값 deep-merge나 label 변경이 운영에서 처음 드러나지 않게 CI에서 고정한다.
python3 scripts/validate_observability.py "$validation_dir"
python3 scripts/validate_edge_platform.py "$validation_dir"

python3 scripts/validate_contracts.py \
  "$validation_dir/prod.yaml" \
  "$validation_dir/dev.yaml" \
  "$validation_dir/preview.yaml" \
  "$validation_dir/secrets.yaml" \
  "$validation_dir/active-edge-test-net.yaml" \
  "$validation_dir/active-preview-edge-test-net.yaml"

python3 - <<'PY'
from pathlib import Path
import yaml

for root in ("bootstrap", "argocd", "manifests"):
    for path in Path(root).rglob("*.yaml"):
        with path.open(encoding="utf-8") as stream:
            list(yaml.safe_load_all(stream))
print("repository YAML is parseable")
PY

for script in scripts/*.sh; do
  bash -n "$script"
done

if command -v cfn-lint >/dev/null; then
  cfn-lint cloud/aws/*.yaml
else
  echo "cfn-lint not installed; CloudFormation lint skipped"
fi

if command -v ansible-playbook >/dev/null; then
  (
    cd ansible
    for playbook in playbooks/tailscale-enroll.yml playbooks/bootstrap.yml; do
      ansible-playbook --syntax-check \
        -i inventories/idc/hosts.example.yml "$playbook"
    done
  )
else
  echo "ansible-playbook not installed; Ansible syntax check skipped"
fi

if command -v kubeconform >/dev/null; then
  find manifests bootstrap argocd -type f -name '*.yaml' -print0 \
    | xargs -0 kubeconform \
      -strict -summary -ignore-missing-schemas -kubernetes-version 1.36.0
  kubeconform -strict -summary -ignore-missing-schemas -kubernetes-version 1.36.0 \
    "$validation_dir/prod.yaml" "$validation_dir/dev.yaml" \
    "$validation_dir/preview.yaml" "$validation_dir/secrets.yaml" \
    "$validation_dir/active-edge-test-net.yaml" \
    "$validation_dir/active-preview-edge-test-net.yaml" \
    "$validation_dir/argocd.yaml" \
    "$validation_dir"/observability-*.yaml \
    "$validation_dir"/platform-*.yaml
else
  echo "kubeconform not installed; Kubernetes schema validation skipped"
fi

if command -v promtool >/dev/null; then
  promtool check rules observability/rules/prometheus-alerts.yaml
  python3 - "$validation_dir/generated-prometheus-rules.yaml" <<'PY'
import sys
from pathlib import Path
import yaml

with Path("manifests/observability/prometheus-alert-rules.yaml").open(encoding="utf-8") as stream:
    configmap = yaml.safe_load(stream)
Path(sys.argv[1]).write_text(configmap["data"]["default-alerts.yml"], encoding="utf-8")
PY
  promtool check rules "$validation_dir/generated-prometheus-rules.yaml"
  promtool check config --syntax-only "$validation_dir/prometheus-config.yaml"
else
  echo "promtool not installed; Prometheus rule validation skipped"
fi

# 실제 배포 image 자체로 각 관측 backend의 최종 병합 config를 검증한다. digest를
# 고정해 CI validator와 runtime parser가 같은 버전이 되도록 한다.
if command -v docker >/dev/null; then
  docker run --rm --network none \
    -v "$validation_dir:/validation:ro" \
    --entrypoint /bin/amtool \
    quay.io/prometheus/alertmanager@sha256:51a825c2a40acc3e338fdd00d622e01ec090f72be2b3ea46be0839cd47a4d286 \
    check-config /validation/alertmanager-config.yaml
  docker run --rm --network none \
    -v "$validation_dir:/validation:ro" \
    --entrypoint /usr/bin/loki \
    grafana/loki@sha256:191d4fdfb7264f16989f0a57f320872620a5a7c2ceeec6229212c4190ec49b86 \
    -config.file=/validation/loki-config.yaml -verify-config=true
  docker run --rm --network none \
    -v "$validation_dir:/validation:ro" \
    --entrypoint /tempo \
    grafana/tempo:2.10.5@sha256:ee21727732c7a7199cb71c3eee9153bbf23f9b0b87619f0555a0cf21a67f1a33 \
    -config.file=/validation/tempo-config.yaml -config.verify=true
  docker run --rm --network none \
    -v "$validation_dir:/validation:ro" \
    otel/opentelemetry-collector-contrib@sha256:f41d7995565df3733b7568702073a9c490792f9c6ac60684fe6a4da21a313f8d \
    validate --config=/validation/otel-collector-config.yaml
else
  echo "docker not installed; observability runtime config validation skipped"
fi

echo "umc-infra validation passed"

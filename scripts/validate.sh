#!/usr/bin/env bash
set -euo pipefail

# 저장소 원본과 렌더 결과를 검증하는 진입점이며 실제 클러스터에 적용하지 않는다.
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
cd "$repo_root"
# shellcheck source=scripts/validate-skips.sh
source "$script_dir/validate-skips.sh"

command -v helm >/dev/null || {
  echo "helm is required" >&2
  exit 1
}
python3 -c 'import yaml' >/dev/null 2>&1 || {
  echo "PyYAML is required" >&2
  exit 1
}

# 지정한 출력 폴더는 보존하고, 이 실행이 만든 임시 폴더만 종료 시 정리한다.
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

# 원본/생성물 불일치와 Python 회귀를 먼저 잡아 뒤의 chart 렌더 실패와 구분한다.
python3 scripts/gen-configmaps.py --check
python3 scripts/validate_preview_releases.py
# 비교할 base가 없는 clone에서는 chart version guard를 건너뛴 검사로 남긴다.
chart_base_ref="${VALIDATE_BASE_REF:-origin/main}"
if optional_check "chart version bump guard" git rev-parse --verify --quiet "$chart_base_ref^{commit}"; then
  python3 scripts/validate_gitops_sources.py --base-ref "$chart_base_ref"
else
  python3 scripts/validate_gitops_sources.py
fi
PYTHONPYCACHEPREFIX="$validation_dir/pycache" python3 -m py_compile scripts/*.py
PYTHONPYCACHEPREFIX="$validation_dir/pycache" \
  python3 -m unittest discover -s scripts/tests -p 'test_*.py'

# 버전이 다른 로컬 Helm이 렌더를 다 돈 뒤에야 실패하지 않게 첫 chart 렌더 전에 확인한다.
python3 scripts/validate_argocd.py --helm-version-only

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

# 실제 ApplicationSet parameter를 사용한다. 별도의 --set fixture가 누락된 PR 운영값을 가리지 않는다.
python3 - "$validation_dir" <<'PY'
import subprocess
import sys
from pathlib import Path

import yaml

appset = yaml.safe_load(Path("argocd/applications/preview/applicationset.yaml").read_text())
source = appset["spec"]["template"]["spec"]["source"]
chart = Path(source["path"])


def router_middlewares(release, *names):
    return ",".join(f"preview-{release}-{name}@kubernetescrd" for name in names)


for number in (42, 43):
    tag = "0123456789ab" if number == 42 else "123456789012"
    digest = "sha256:" + "a" * 64
    name = f"umc-product-server-pr-{number}"
    args = ["helm", "template", f"umc-product-preview-{number}", str(chart), "--namespace", "preview"]
    for value_file in source["helm"]["valueFiles"]:
        args.extend(["-f", str(chart / value_file)])
    for parameter in source["helm"]["parameters"]:
        value = parameter["value"].replace("{{ .number }}", str(number))
        value = value.replace("{{ .release.imageTag }}", tag)
        value = value.replace("{{ .release.imageDigest }}", digest)
        if "{{" in value:
            raise SystemExit(f"unsupported Preview template expression: {parameter['name']}")
        flag = "--set-string" if parameter.get("forceString") else "--set"
        args.extend([flag, f"{parameter['name']}={value}"])
    result = subprocess.run(args, check=True, capture_output=True, text=True)
    Path(sys.argv[1], f"preview-pr-{number}-desired-state.yaml").write_text(result.stdout)
    documents = [item for item in yaml.safe_load_all(result.stdout) if item]
    deployment, = [item for item in documents if item["kind"] == "Deployment"]
    ingress, = [item for item in documents if item["kind"] == "Ingress" and item["metadata"]["name"] == name]
    docs, = [item for item in documents if item["kind"] == "Ingress" and item["metadata"]["name"] == name + "-docs"]
    middleware, = [item for item in documents if item["kind"] == "Middleware" and item["metadata"]["name"] == name + "-docs-basic-auth"]
    test_api, = [item for item in documents if item["kind"] == "Ingress" and item["metadata"]["name"] == name + "-test-api"]
    test_api_middleware, = [item for item in documents if item["kind"] == "Middleware" and item["metadata"]["name"] == name + "-test-api-basic-auth"]
    assert test_api_middleware["spec"]["basicAuth"] == {"secret": "docs-basic-auth", "removeHeader": True}
    assert test_api["metadata"]["annotations"]["traefik.ingress.kubernetes.io/router.middlewares"] == router_middlewares(name, "rate-limit", "in-flight", "test-api-basic-auth")
    assert [item["path"] for item in test_api["spec"]["rules"][0]["http"]["paths"]] == ["/test"]
    assert deployment["metadata"]["name"] == ingress["metadata"]["name"] == name
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == f"ghcr.io/umc-product/umc-product-server@{digest}"
    assert middleware["spec"]["basicAuth"] == {"secret": "docs-basic-auth", "removeHeader": True}
    assert docs["metadata"]["annotations"]["traefik.ingress.kubernetes.io/router.middlewares"] == router_middlewares(name, "rate-limit", "in-flight", "docs-basic-auth")
    assert [item["path"] for item in docs["spec"]["rules"][0]["http"]["paths"]] == ["/docs", "/docs-json"]
    # 일반 API에는 Basic Auth 없이 edge 제한만 붙는다.
    assert ingress["metadata"]["annotations"]["traefik.ingress.kubernetes.io/router.middlewares"] == router_middlewares(name, "rate-limit", "in-flight")
    websocket, = [item for item in documents if item["kind"] == "Ingress" and item["metadata"]["name"] == name + "-ws"]
    assert websocket["metadata"]["annotations"]["traefik.ingress.kubernetes.io/router.middlewares"] == router_middlewares(name, "rate-limit", "ws-in-flight")
    assert "docs-basic-auth" not in [item["secretRef"]["name"] for item in container["envFrom"]]
    env = {item["name"]: item.get("value") for item in container["env"]}
    assert env["DEMODAY_QR_BASE_URL"] == "https://university.neordinary.com"
    assert env["DATABASE_URL"].endswith(f"/umc_product_pr{number}")
    assert env["OPENAPI_ENABLE"] == "true"
    assert env["APP_TEST_API_ENABLED"] == env["APP_SEED_ENABLED"] == "true"
    assert ingress["spec"]["rules"][0]["host"] == f"api-pr-{number}.university.neordinary.com"
    assert ingress["spec"]["tls"][0]["secretName"] == "preview-wildcard-tls"
    assert len([item for item in documents if item["kind"] == "Job"]) == 2
    # 누락된 발행 digest로는 Preview를 열지 못한다.
    invalid = subprocess.run(args + ["--set-string", "image.digest="], capture_output=True, text=True)
    assert invalid.returncode != 0
print("Preview ApplicationSet renders digest-pinned images, edge limits and protected docs·test API for both PRs")
PY

# test API 리소스는 testApi 값만으로 노출되지 않고 Deployment와 Ingress gate를 모두 따른다.
helm template dev-umc-product-server charts/umc-product-server \
  --namespace dev-app \
  -f charts/umc-product-server/values-dev.yaml \
  --set ingress.enabled=false \
  >"$validation_dir/dev-test-api-ingress-disabled.yaml"
helm template dev-umc-product-server charts/umc-product-server \
  --namespace dev-app \
  -f charts/umc-product-server/values-dev.yaml \
  --set deployment.enabled=false \
  --set ingress.enabled=false \
  >"$validation_dir/dev-test-api-deployment-disabled.yaml"
python3 - \
  "$validation_dir/dev-test-api-ingress-disabled.yaml" \
  "$validation_dir/dev-test-api-deployment-disabled.yaml" <<'PY'
import sys
from pathlib import Path

import yaml

protected_resource_suffixes = ("-test-api", "-test-api-basic-auth")
for filename in sys.argv[1:]:
    with Path(filename).open(encoding="utf-8") as stream:
        names = {
            document.get("metadata", {}).get("name")
            for document in yaml.safe_load_all(stream)
            if isinstance(document, dict)
        }
    if any(
        name and name.endswith(protected_resource_suffixes)
        for name in names
    ):
        raise SystemExit(f"test API resource bypassed deployment/ingress gate: {filename}")
PY

# 실제 FE origin을 넣지 않으면 workload gate가 열리지 않아야 한다.
if helm template umc-product-server charts/umc-product-server \
  -f charts/umc-product-server/values-prod.yaml "${common_args[@]}" \
  --set-string env.DEMODAY_QR_BASE_URL=https://bootstrap-required.example.invalid \
  >/dev/null 2>&1; then
  echo "DEMODAY_QR_BASE_URL bootstrap gate unexpectedly passed" >&2
  exit 1
fi

# prod/dev는 FCM을 끌 수 없고, FCM을 켠 Pod에는 Firebase Secret이 반드시 필요하다.
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
  --set-string env.FCM_ENABLED=false >/dev/null 2>&1; then
  echo "dev FCM_ENABLED=false unexpectedly passed" >&2
  exit 1
fi
if helm template dev-umc-product-server charts/umc-product-server \
  -f charts/umc-product-server/values-dev.yaml "${common_args[@]}" \
  --set 'requiredSecrets={app-db,app-jwt,app-oauth,app-storage,app-email}' \
  >/dev/null 2>&1; then
  echo "dev FCM without app-fcm unexpectedly passed" >&2
  exit 1
fi

# test API의 애플리케이션 gate와 보호 Ingress는 함께 켜지고 dev에서만 허용되어야 한다.
if helm template dev-umc-product-server charts/umc-product-server \
  -f charts/umc-product-server/values-dev.yaml "${common_args[@]}" \
  --set-string env.APP_TEST_API_ENABLED=false >/dev/null 2>&1; then
  echo "test API Ingress without runtime gate unexpectedly passed" >&2
  exit 1
fi
if helm template dev-umc-product-server charts/umc-product-server \
  -f charts/umc-product-server/values-dev.yaml "${common_args[@]}" \
  --set testApi.enabled=false >/dev/null 2>&1; then
  echo "test API runtime gate without protected Ingress unexpectedly passed" >&2
  exit 1
fi
if helm template umc-product-server charts/umc-product-server \
  -f charts/umc-product-server/values-prod.yaml "${common_args[@]}" \
  --set testApi.enabled=true \
  --set-string env.APP_TEST_API_ENABLED=true >/dev/null 2>&1; then
  echo "production test API unexpectedly passed" >&2
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
  --set-string env.EMAIL_PROVIDER=ses \
  --set-string env.EMAIL_NO_REPLY_ADDRESS=no-reply-nonprod@university.neordinary.com \
  >/dev/null 2>&1; then
  echo "prod nonprod sender address unexpectedly passed" >&2
  exit 1
fi
if helm template dev-umc-product-server charts/umc-product-server \
  -f charts/umc-product-server/values-dev.yaml "${common_args[@]}" \
  --set-string env.EMAIL_PROVIDER=ses \
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

# Traefik은 0을 무제한으로 해석하므로 edge 한도 0은 렌더 단계에서 거부한다.
for edge_override in edgeProtection.rateLimit.average=0 edgeProtection.rateLimit.burst=0 edgeProtection.inFlight.amount=0 edgeProtection.webSocketInFlight.amount=0; do
  if helm template umc-product-server charts/umc-product-server \
    -f charts/umc-product-server/values-prod.yaml "${common_args[@]}" \
    --set "$edge_override" >/dev/null 2>&1; then
    echo "unlimited edge setting $edge_override unexpectedly passed" >&2
    exit 1
  fi
done

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
  --set-string fullnameOverride=umc-product-server-pr-42
  --set-string createDatabase.name=umc_product_pr42
  --set-string env.DATABASE_URL=jdbc:postgresql://postgres-preview.preview.svc.cluster.local:5432/umc_product_pr42
  --set-string env.SPRING_APPLICATION_NAME=preview-umc-product-pr42
  --set-string env.APP_ENVIRONMENT=preview-pr-42
  --set-string env.SSO_ISSUER=https://api-pr-42.university.neordinary.com
  --set-string 'env.CERTIFICATE_VERIFICATION_URL_TEMPLATE=https://api-pr-42.university.neordinary.com/api/v1/certificates/verify/{serialNumber}'
  --set-string ingress.host=api-pr-42.university.neordinary.com
  --set-string 'ingress.tls[0].hosts[0]=api-pr-42.university.neordinary.com'
  --set-string mailCapture.uiHost=mail-pr-42.university.neordinary.com
)
# Preview는 FCM 활성화와 Firebase Secret 주입을 모두 금지한다.
if helm template umc-product-preview-42 charts/umc-product-server \
  -f charts/umc-product-server/values-preview.yaml "${preview_args[@]}" \
  --set-string env.FCM_ENABLED=true >/dev/null 2>&1; then
  echo "preview FCM_ENABLED=true unexpectedly passed" >&2
  exit 1
fi
if helm template umc-product-preview-42 charts/umc-product-server \
  -f charts/umc-product-server/values-preview.yaml "${preview_args[@]}" \
  --set 'requiredSecrets={app-db,app-jwt,app-oauth,app-storage,app-email,app-fcm}' \
  >/dev/null 2>&1; then
  echo "preview app-fcm unexpectedly passed" >&2
  exit 1
fi
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
python3 scripts/validate_edge_platform.py "$validation_dir"
python3 scripts/validate_observability.py "$validation_dir"

# 위 platform·observability 렌더까지 모아 AppProject whitelist를 모든 Application 렌더와 대조한다.
python3 scripts/argocd_project_kinds.py "$validation_dir"

# 개별 YAML 문법만으로 알 수 없는 환경 간 Secret·DB·DNS 연결 계약을 확인한다.
python3 scripts/validate_contracts.py \
  "$validation_dir/prod.yaml" \
  "$validation_dir/dev.yaml" \
  "$validation_dir/preview.yaml" \
  "$validation_dir/secrets.yaml" \
  "$validation_dir/active-edge-test-net.yaml" \
  "$validation_dir/active-preview-edge-test-net.yaml" \
  "$validation_dir/preview-pr-42-desired-state.yaml" \
  "$validation_dir/observability-grafana.yaml"

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

if optional_check "CloudFormation lint" command -v cfn-lint; then
  cfn-lint cloud/aws/*.yaml
fi

# 실제 inventory 대신 공개 예제로 구문만 검사하며 서버 접속이나 적용은 수행하지 않는다.
if optional_check "Ansible syntax check" command -v ansible-playbook; then
  (
    cd ansible
    for playbook in playbooks/*.yml; do
      ansible-playbook --syntax-check \
        -i inventories/idc-new/hosts.example.yml "$playbook"
    done
  )
fi
if optional_check "Ansible lint" command -v ansible-lint; then
  (cd ansible && ansible-lint playbooks/ssh-access.yml playbooks/bootstrap.yml)
fi

if optional_check "Kubernetes schema validation" command -v kubeconform; then
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
  # Operator 사용자 리소스는 고정 CRD 스키마로 누락 없이 검사한다.
  # admission/CEL 검증은 별도의 서버 dry-run이 필요하다.
  kubeconform -strict -summary \
    -schema-location "$validation_dir/monitoring-schemas/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json" \
    "$validation_dir/monitoring-resources.yaml"
fi

if optional_check "Prometheus rule validation" command -v promtool; then
  promtool check rules --lint-fatal observability/rules/prometheus-alerts.yaml
  # Operator가 런타임에 조립할 설정 대신 chart 기본값을 포함한 모든 렌더 규칙을 검사한다.
  for rule_file in "$validation_dir"/prometheus-rules-*.yaml; do
    if [[ "${rule_file##*/}" == prometheus-rules-prometheus-alert-rules.yaml ]]; then
      promtool check rules --lint-fatal "$rule_file"
    else
      # upstream은 같은 이름으로 서로 다른 응답 코드 범위를 기록하므로 lint 경고는 허용한다.
      # 구문 오류는 그대로 실패시킨다.
      promtool check rules "$rule_file"
    fi
  done
fi

# 실제 배포 image로 backend config를 검증한다. Alertmanager config는 Operator가
# 생성하므로 native CRD schema와 live reconcile/readiness로 별도 확인한다.
if optional_check "observability runtime config validation" docker info; then
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
fi

finish_validation

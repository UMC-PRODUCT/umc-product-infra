# GitHub trust root

Argo CD가 `UMC-PRODUCT/umc-product-infra`의 `main`을 자동 반영하므로 branch write는 사실상 cluster-admin 권한이다.
저장소의 public 여부와 write 권한은 별개다.

## 권장 보호 규칙

| 정책 | 값 |
|---|---|
| 일반 변경 경로 | pull request only |
| 배포 자동화 | 승인된 bot의 `values-prod.yaml` / `values-dev.yaml` direct push만 예외 |
| direct push 사전 검증 | 선택한 환경의 `helm lint --strict` + `helm template` |
| push 후 검증 | `Static validation` |
| approval | 최소 1명 |
| stale review | 새 push 때 dismiss |
| last push | 마지막 push 작성자 외 승인 필요 |
| bypass | 배포 bot만 명시적 허용 |
| history | squash + linear |
| conversation resolution | 필수 |
| force push / branch delete | 금지 |

일반 인프라 변경은 클러스터 권한 변경이므로 승인 0을 금지한다. 예외는 trusted backend
branch에서 실행되고, 선택한 image values 파일만 갱신하며, push 전 Helm 검증을
통과한 배포 자동화다. UMC team slug 확정 후 CODEOWNERS team을 추가하되, 일반
변경에는 그전에도 1명 승인을 유지한다.

workflow 표시명은 `Validate infrastructure`, check context는 job `Static validation`이다. direct push 후
실행되는 이 check는 Argo CD 동기화를 사전 차단하지 못하므로, backend workflow의
정확한 파일 제한과 Helm 사전 검증이 배포 gate다.

## 적용 순서

1. `UMC-PRODUCT/umc-product-infra` 저장소를 만들고 초기 버전을 `main`에 올린다.
2. `Static validation`이 실제 `main` SHA에서 성공했는지 확인한다.
3. GitHub secret scanning과 push protection을 켠다.
4. backend 배포 bot용 fine-grained PAT에 해당 저장소의 `Contents: Read and write`만 부여하고
   backend 저장소의 `UMC_INFRA_GITOPS_TOKEN` Secret으로 저장한다.
5. 일반 변경은 PR로 제한하되 해당 배포 bot만 direct push 예외로 명시한다.

이 정책은 GitHub 저장소 Settings에서 관리한다. 배포 bot 예외 없이 PR-only 보호 규칙을
적용하면 backend 자동 배포의 infra `main` push가 거부되므로, 둘을 반드시 함께 설정한다.

조직 2FA 강제와 복구 수단 관리는 권장하지만 조직 owner가 별도로 다룰 정책이다. 저장소 branch
protection 적용과는 별도로 관리한다.

## Backend 배포 자동화 계약

- prod source: `main`
- dev source: `develop`
- preview source: `develop` 대상의 trusted same-repository PR + `preview` label
- image: public `ghcr.io/umc-product/umc-product-server:<12-char-sha>`; cluster는 credential 없이 anonymous pull
- prod/dev deploy: registry digest 확인 → 선택 values 갱신 → Helm 사전 검증 → infra `main` direct push
- preview: PR head SHA tag 게시; 외부/fork PR에는 credential workflow 실행 금지
- infra write credential: `umc-product-infra` 한 저장소만 허용한 fine-grained token 또는 GitHub App
- permission: `Contents: Read and write`만 부여, 만료와 회전 적용
- updater는 `values-prod.yaml` 또는 `values-dev.yaml` 중 선택된 파일 하나 외의 변경을 거부
- prod/dev 갱신 job은 같은 concurrency group으로 infra `main` push를 직렬화
- push 성공 후 Argo CD가 새 desired state를 감지해 자동 동기화

현재 차트는 `deployment.enabled: false`이고 image digest가 비어 있으므로 첫 image direct push
전에는 앱을 만들지 않는다. updater는 첫 image를 기록할 때만 Deployment와 Ingress gate를
함께 열고, 그 상태를 사전 렌더링해 성공한 경우에만 push한다.

## 공개 PR 경계

일반 infra PR validation은 read-only `GITHUB_TOKEN`과 GitHub-hosted runner만 쓴다. infra write PAT는
trusted `main` / `develop` push의 backend `update-infra` job에만 제공한다. fork PR code에 AWS,
cluster, deploy credential을 주지 않는다. GHCR package가 public이어도 fork code를 trusted publish
workflow에서 실행하거나 image를 배포하지 않는다.

preview의 `preview` label은 maintainer의 trust 승인이다. ApplicationSet은 author association을 필터링하지 못하므로 backend CI가 same-repository와 author association을 먼저 fail-closed로 검사해야 한다.

## 확인

```bash
gh api repos/UMC-PRODUCT/umc-product-infra/branches/main/protection
gh api repos/UMC-PRODUCT/umc-product-infra \
  --jq '{allow_auto_merge,allow_squash_merge,allow_merge_commit,allow_rebase_merge,delete_branch_on_merge}'
```

출력이 위 보호 규칙과 다음 조건을 만족해야 한다.

- 일반 PR의 required check는 `Static validation`과 GitHub Actions App ID에 묶여야 한다.
- approval 1, stale review dismissal, last-push approval은 on이다.
- direct push bypass는 배포 bot 주체에만 허용한다.
- linear history와 conversation resolution은 on이다.
- force push와 branch deletion은 off다.
- merge commit과 rebase merge는 off다.

영구 rollback은 known-good tag+digest를 복원하고 사전 검증한 infra `main` commit으로 한다.
Argo CD UI rollback은 self-heal이 최신 Git으로 돌려놓으므로 임시 조치다.

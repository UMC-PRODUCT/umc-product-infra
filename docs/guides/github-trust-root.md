# GitHub trust root

Argo CD가 `UMC-PRODUCT/umc-product-infra`의 `main`을 자동 반영하므로 branch write는 사실상 cluster-admin 권한이다.
저장소의 public 여부와 write 권한은 별개다.

## 권장 보호 규칙

| 정책 | 값 |
|---|---|
| merge 경로 | pull request only |
| required check | `Static validation`, strict |
| approval | 최소 1명 |
| stale review | 새 push 때 dismiss |
| last push | 마지막 push 작성자 외 승인 필요 |
| admin bypass | 금지 |
| history | squash + linear |
| conversation resolution | 필수 |
| force push / branch delete | 금지 |

인프라 변경은 클러스터 권한 변경이므로 승인 0을 금지한다. UMC team slug 확정 후 CODEOWNERS team을 추가하되, 그전에도 1명 승인을 유지한다.

workflow 표시명은 `Validate infrastructure`, required-check context는 job `Static validation`이다. 스크립트는 임의 동명 status가 아닌 현재 `main` SHA에서 성공한 check의 GitHub Actions App ID까지 확인한다.

## 적용 순서

1. `UMC-PRODUCT/umc-product-infra` 저장소를 만들고 초기 버전을 `main`에 올린다.
2. `Static validation`이 실제 `main` SHA에서 성공했는지 확인한다.
3. GitHub secret scanning과 push protection을 켠다.
4. 관리자 환경에서 `gh auth status`와 `jq`를 확인한다.
5. 아래 명령을 실행한다.

```bash
scripts/configure-github-trust-root.sh --apply
```

스크립트는 `umc-product-server`를 읽거나 수정하지 않아 backend publish workflow 전에도 infra를 보호한다.
branch protection 후 auto-merge, squash-only, merge 후 branch 삭제를 설정한다. 중간 실패해도 보호는 남으며 같은 명령으로 수렴한다.

조직 2FA 강제와 복구 수단 관리는 권장하지만 조직 owner가 별도로 다룰 정책이다. 저장소 branch
protection 적용과 결합하지 않으며, 이 스크립트도 조직 구성원이나 2FA 상태를 조회하지 않는다.

## Backend 자동화의 미래 계약

- prod source: `main`
- dev source: `develop`
- preview source: `develop` 대상의 trusted same-repository PR + `preview` label
- image: public `ghcr.io/umc-product/umc-product-server:<12-char-sha>`; cluster는 credential 없이 anonymous pull
- prod/dev deploy: registry digest 확인 후 infra image values PR 생성
- preview: PR head SHA tag 게시; 외부/fork PR에는 credential workflow 실행 금지
- infra write credential: `umc-product-infra` 한 저장소만 허용한 fine-grained token 또는 GitHub App
- permission: Contents/Pull requests 최소 read/write, 만료와 회전 적용
- direct push 금지; 봇도 PR과 `Static validation`을 통과

현재 차트는 `deployment.enabled: false`이고 image digest가 비어 있으므로 이 계약을 구현하기 전에는 앱을 만들지 않는다.

## 공개 PR 경계

infra PR validation은 read-only `GITHUB_TOKEN`과 GitHub-hosted runner만 쓴다. fork PR code에 AWS, cluster, deploy credential을 주지 않는다. GHCR package가 public이어도 fork code를 trusted publish workflow에서 실행하거나 image를 배포하지 않는다.

preview의 `preview` label은 maintainer의 trust 승인이다. ApplicationSet은 author association을 필터링하지 못하므로 backend CI가 same-repository와 author association을 먼저 fail-closed로 검사해야 한다.

## 확인

```bash
gh api repos/UMC-PRODUCT/umc-product-infra/branches/main/protection
gh api repos/UMC-PRODUCT/umc-product-infra \
  --jq '{allow_auto_merge,allow_squash_merge,allow_merge_commit,allow_rebase_merge,delete_branch_on_merge}'
```

출력이 위 보호 규칙과 다음 조건을 만족해야 한다.

- strict required check는 `Static validation`과 GitHub Actions App ID에 묶여야 한다.
- admin enforcement, approval 1, stale review dismissal, last-push approval은 on이다.
- linear history와 conversation resolution은 on이다.
- force push와 branch deletion은 off다.
- merge commit과 rebase merge는 off다.

영구 rollback은 known-good tag+digest 복원 PR로 한다. Argo CD UI rollback은 self-heal이 최신 Git으로 돌려놓으므로 임시 조치다.

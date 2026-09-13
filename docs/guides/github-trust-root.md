# GitHub trust root

Argo CD가 `UMC-PRODUCT/umc-product-infra`의 `main`을 자동 반영하므로 branch write는 사실상 cluster-admin 권한이다.
저장소의 public 여부와 write 권한은 별개다.

## 권장 보호 규칙

| 정책 | 값 |
|---|---|
| 일반 변경 경로 | pull request only |
| 배포 자동화 | 승인된 bot의 환경별 image values 또는 해당 PR의 성공 release 파일 갱신만 예외 |
| direct push 사전 검증 | prod/dev는 선택한 환경의 `helm lint --strict` + `helm template`; Preview는 성공 image 검증·신뢰된 PR 재확인·선택 파일 제한 |
| push 후 검증 | `Static validation` |
| approval | 최소 1명 |
| stale review | 새 push 때 dismiss |
| last push | 마지막 push 작성자 외 승인 필요 |
| bypass | 배포 bot만 명시적 허용 |
| history | squash + linear |
| conversation resolution | 필수 |
| force push / branch delete | 금지 |

일반 인프라 변경은 클러스터 권한 변경이므로 승인 0을 금지한다. 예외는 아래 계약에 맞게
검증한 이미지 정보만 기록하는 배포 자동화다. CODEOWNERS team은 GitHub의 실제 UMC team slug로 지정하되,
일반 변경은 CODEOWNERS 설정 여부와 관계없이 1명 승인을 유지한다.

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

아래는 backend workflow가 지켜야 하는 계약이다. 구현과 실행 결과는 backend 저장소의
`.github/workflows/`와 Actions run에서 확인하고, infra 저장소의 선언만으로 완료를 판단하지 않는다.

- prod source: `main`
- dev source: `develop`
- preview source: `develop` 대상의 trusted same-repository PR + `preview` label
- image: public `ghcr.io/umc-product/umc-product-server:<12-char-sha>`; cluster는 credential 없이 anonymous pull
- prod/dev deploy: registry digest 확인 → 선택 values 갱신 → Helm 사전 검증 → infra `main` direct push
- preview: trusted PR image 발행·익명 pull 검증 → 해당 PR의 `argocd/preview-releases/pr-<번호>.json`에 SHA·tag·digest 기록
  → ApplicationSet이 성공 release를 소비하는지 확인. 상세 전제는 [Preview 가이드](preview-environments.md#사용-조건)를 따른다.
- infra write credential: `umc-product-infra` 한 저장소만 허용한 fine-grained token 또는 GitHub App
- permission: `Contents: Read and write`만 부여, 만료와 회전 적용
- prod/dev updater는 `values-prod.yaml` 또는 `values-dev.yaml` 중 선택된 파일 하나 외의 변경을 거부
- Preview updater는 해당 PR의 성공 release 파일 하나 외의 변경을 거부하고 push 직전 PR 상태·head SHA·신뢰 조건을 재확인
- prod/dev/preview 갱신 job은 같은 concurrency group으로 infra `main` push를 직렬화
- push 성공 후 Argo CD가 새 desired state를 감지해 자동 동기화

배포 gate와 image는 [공통 values](../../charts/umc-product-server/values.yaml)에 환경별
[prod](../../charts/umc-product-server/values-prod.yaml)·[dev](../../charts/umc-product-server/values-dev.yaml)를
합친 값으로 확인한다. 공통값의 비활성 gate와 빈 digest만으로 실제 환경의 상태를 판단하지 않는다.
updater는 첫 image를 기록할 때만 Deployment와 Ingress gate를 함께 열고, 그 상태를 사전
렌더링해 성공한 경우에만 push해야 한다. 실제 갱신 범위는 backend workflow와 infra commit에서 확인한다.

## 공개 PR 경계

일반 infra PR validation은 read-only `GITHUB_TOKEN`과 GitHub-hosted runner만 쓴다.
infra write PAT는 prod/dev의 trusted branch 갱신 job 또는 Preview의 성공 이미지 기록 job에만 제공한다.
Preview는 PR 코드를 빌드하는 job과 credential을 쓰는 job을 분리한다. 성공 이미지 기록 job은
기본 브랜치의 신뢰된 updater만 실행하며 PR head의 script·action·artifact를 실행하지 않는다.
fork PR code에 AWS, cluster, deploy credential을 주지 않는다. GHCR package가 public이어도
fork code를 trusted publish workflow에서 실행하거나 image를 배포하지 않는다.

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
자동 동기화·self-heal 환경에서는 Git 변경 없는 UI 조작을 영구 복구 절차로 사용하지 않는다.

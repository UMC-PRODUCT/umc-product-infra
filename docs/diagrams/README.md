# 인프라 다이어그램

이 디렉터리는 UMC 인프라의 **구조 설명 그림**을 관리한다. 그림을 보기만 한다면 아래 목록을
열면 되고, 수정·재생성할 때만
[다이어그램 유지보수 가이드](../guides/diagram-maintenance.md)를 따른다.

> [!NOTE]
> 그림은 실시간 배포 상태가 아니다. 활성화 조건은 환경별 values와 가이드에서,
> 리소스 목록·개수·동기화 순서는 manifest에서 확인한다.

## 무엇을 하려는가

| 목적 | 시작 위치 |
|---|---|
| 인프라 구조를 이해하고 싶다 | 아래 목표 구조 그림을 순서대로 본다 |
| Python 그림을 수정하고 싶다 | [목표 구조 그림 재생성](../guides/diagram-maintenance.md#2-목표-구조-그림-재생성) |

## 목표 구조 그림

처음에는 `traffic-flow`로 사용자 요청 경로를 보고, `gitops-tree`로 배포 주체를 본 뒤,
`secret-supply-chain`으로 비밀값 경계를 확인하는 순서가 가장 이해하기 쉽다.

| 그림 | 답하는 질문 | PNG | Source |
|---|---|---|---|
| Traffic flow | 사용자 요청이 앱·DB·관측으로 어떻게 흐르는가? | [PNG](out/traffic-flow.png) | [`traffic_flow.py`](traffic_flow.py) |
| GitOps tree | Argo CD가 어떤 Application을 어떤 경계로 관리하는가? | [PNG](out/gitops-tree.png) | [`gitops_tree.py`](gitops_tree.py) |
| Secret supply chain | AWS의 비밀값이 ESO를 거쳐 workload까지 어떻게 오는가? | [PNG](out/secret-supply-chain.png) | [`secret_supply_chain.py`](secret_supply_chain.py) |
| CI/CD flow | Backend image가 GitOps 배포로 어떻게 연결되는가? | [PNG](out/cicd-flow.png) | [`cicd_flow.py`](cicd_flow.py) |
| Branch flow | develop·prod 승격과 rollback은 어떻게 흐르는가? | [PNG](out/branch-flow.png) | [`branch_flow.py`](branch_flow.py) |
| Preview environment | 승인된 PR의 생성·삭제 lifecycle은 무엇인가? | [PNG](out/preview-env.png) | [`preview_env.py`](preview_env.py) |

선의 의미는 각 연결의 라벨을 기준으로 읽는다. 점선에는 DNS 조회·Git 조회·Secret 참조도 포함되므로
선 모양만으로 활성 여부를 판단하지 않는다.

그림을 읽을 때 함께 확인할 차이:

- GitOps 그림의 고정 개수와 wave 요약은 전체 목록이 아니다. Reloader·Argo 접속 구성·모니터링 통합을
  포함한 목록은 [`argocd/applications/`](../../argocd/applications/)와 [프로젝트 선언](../../argocd/projects.yaml)을 확인한다.
- SecretStore·ExternalSecret·AWS source 개수는 [Secret 매핑 원본](../../charts/umc-secrets/values.yaml)을 기준으로 한다.
- Preview 그림의 PR별 인증서 대신 [공용 wildcard 인증서](../../manifests/cert-manager/preview-wildcard-certificate.yaml)를 사용한다.
  PR별 앱·DB 삭제와 공용 인증서 수명주기는 분리된다.
- Branch 그림의 UI rollback 대신 정상 image tag·digest를 Git에 반영하는
  [복구 절차](../guides/github-trust-root.md)를 따른다. DB 복원은 이미지 rollback과 별개다.

## 파일 구조

```text
docs/diagrams/
├── *_flow.py, *_tree.py, *_env.py  # 목표 구조 source
├── theme.py                         # 공통 style
└── out/                              # commit하는 PNG
```

목표 구조 script를 바꿨다면 PNG를 함께 재생성한다. `.venv/`는 commit하지 않는다.

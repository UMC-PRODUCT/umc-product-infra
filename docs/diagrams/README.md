# 인프라 다이어그램

이 디렉터리는 UMC 인프라의 **최종 목표 구조 그림**을 관리한다. 그림을 보기만 한다면 아래 목록을
열면 되고, 수정·재생성할 때만
[다이어그램 유지보수 가이드](../guides/diagram-maintenance.md)를 따른다.

> [!NOTE]
> 목표 구조 그림과 현재 manifest는 모두 `university.neordinary.com` 도메인 계약을 표현한다.

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
| Secret supply chain | 로컬 원장에서 AWS를 거쳐 Pod까지 값이 어떻게 오는가? | [PNG](out/secret-supply-chain.png) | [`secret_supply_chain.py`](secret_supply_chain.py) |
| CI/CD flow | Backend image가 GitOps 배포로 어떻게 연결되는가? | [PNG](out/cicd-flow.png) | [`cicd_flow.py`](cicd_flow.py) |
| Branch flow | develop·prod 승격과 rollback은 어떻게 흐르는가? | [PNG](out/branch-flow.png) | [`branch_flow.py`](branch_flow.py) |
| Preview environment | 승인된 PR의 생성·삭제 lifecycle은 무엇인가? | [PNG](out/preview-env.png) | [`preview_env.py`](preview_env.py) |

실선은 항상 존재하는 경로이고 점선은 인증서, 승인, restore 같은 gate를 통과한 뒤 활성화되는
경로다. 세부 조건은 각 그림 주변 설명보다 실제 manifest와 가이드를 기준으로 확인한다.

## 파일 구조

```text
docs/diagrams/
├── *_flow.py, *_tree.py, *_env.py  # 목표 구조 source
├── theme.py                         # 공통 style
└── out/                              # commit하는 PNG
```

목표 구조 script를 바꿨다면 PNG를 함께 재생성한다. `.venv/`는 commit하지 않는다.

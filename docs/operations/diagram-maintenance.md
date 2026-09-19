# 인프라 다이어그램 유지보수

이 문서는 `docs/diagrams/`의 목표 구조 그림을 재생성하는 절차서다. 그림의 의미와 목록은
[다이어그램 README](../diagrams/README.md)에서 먼저 확인한다.

목표 구조 그림과 현재 manifest는 `university.neordinary.com` 구조를 표현한다.

## 1. 도구 설치

현재 명령은 macOS와 Homebrew 기준이다.

```bash
brew install uv graphviz

cd docs/diagrams
uv venv --python 3.12

export CFLAGS="-I$(brew --prefix graphviz)/include"
export LDFLAGS="-L$(brew --prefix graphviz)/lib"

uv pip install --python .venv/bin/python \
  "diagrams==0.25.1"
```

Python 3.12를 사용한다. `uv pip install`의 `--python .venv/bin/python`을 생략하면 다른 Python
환경에 설치될 수 있다.

## 2. 목표 구조 그림 재생성

`docs/diagrams/`에서 실행한다.

```bash
.venv/bin/python cicd_flow.py
.venv/bin/python branch_flow.py
.venv/bin/python traffic_flow.py
.venv/bin/python gitops_tree.py
.venv/bin/python preview_env.py
.venv/bin/python secret_supply_chain.py
```

각 script는 `out/`에 PNG 하나를 만든다. `theme.py`가 공통 글꼴, 색상과
Graphviz 속성을 가진다.

선의 의미는 각 연결의 라벨로 읽는다. DNS 조회·Git 조회·Secret 참조에도 점선을 사용하므로
선 모양만으로 활성 여부를 판단하지 않는다. 다음 기능은 별도 운영 조건을 검증한다.

- 앱 Ingress: Route 53 exact record와 host Certificate, direct HTTPS 검증 후 활성화
- Grafana Ingress: production Certificate, 실제 IDC IP, login 필수 상태를 검증한 후 활성화
- backup: 외부 restore rehearsal 후 `suspend: false`
- 외부 uptime monitor: 클러스터 밖에서 별도 생성

Preview 그림의 `preview` label은 trusted same-repository PR 검토가 끝났다는 승인이다. Preview는
shared Secret과 shared DB role을 사용하므로 fork와 외부 코드를 실행하지 않는다. 최종 목표 API는
`api-pr-<PR>.university.neordinary.com`이고 동시 상한은 3개다. 모바일 native token login은
지원하지만 browser OAuth callback broker는 운영하지 않는다.

### 변경 확인

```bash
git diff -- docs/diagrams
file out/*.png
```

이미지를 열어 글자가 잘리거나 선이 겹치지 않는지 확인한다.

## 3. Commit 대상

| 경로 | Commit | 설명 |
|---|---|---|
| `docs/diagrams/*.py` | 예 | 목표 구조 source |
| `docs/diagrams/theme.py` | 예 | 공통 style |
| `docs/diagrams/out/*.png` | 예 | 검토된 최종 그림 |
| `.venv/` | 아니요 | 로컬 Python 환경 |

Python flow script를 바꿨다면 대응하는 PNG를 같은 commit에 포함한다.

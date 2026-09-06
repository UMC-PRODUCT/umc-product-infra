"""CI/CD 배포 흐름 — 코드 push 부터 클러스터 반영까지.

매니페스트만으로는 그릴 수 없다. "Actions 가 ghcr 에 밀고 인프라 tag+digest PR을 열면 필수 CI 뒤
merge되고 ArgoCD 가 감지한다"는 어떤 매니페스트에도 적혀 있지 않기 때문이다. 그래서 손으로 그린다.

이 그림이 반드시 전달해야 하는 두 가지:
  1) ArgoCD → GitHub 방향이 pull 이다 (push 아님)
  2) 앱 레포에서 클러스터로 가는 화살표와 infra main 직접 push가 없다

    python cicd_flow.py     →  out/cicd-flow.png
"""

from diagrams import Cluster, Diagram, Edge
from diagrams.k8s.compute import Deployment
from diagrams.onprem.ci import GithubActions
from diagrams.onprem.client import User
from diagrams.onprem.container import Docker
from diagrams.onprem.gitops import Argocd
from diagrams.onprem.vcs import Git, Github

from theme import THEME, cluster_attr, edge_attr, graph_attr, node_attr

PULL = "#E5534B"  # pull 방향 강조


def build(theme: dict) -> None:
    ca = cluster_attr(theme)
    with Diagram(
        "UMC Product 배포 흐름 (GitOps)",
        filename="out/cicd-flow",
        outformat="png",
        show=False,
        graph_attr=graph_attr(theme, rankdir="LR", ranksep="1.4"),
        node_attr=node_attr(theme),
        edge_attr=edge_attr(theme),
    ):
        dev = User("개발자")

        with Cluster("umc-product-server  (public)", graph_attr=ca):
            repo_be = Github("main / develop")
            actions = GithubActions("trusted publish CI\n브랜치로 환경 판정")

        registry = Docker(
            "public ghcr.io/umc-product/umc-product-server\nSHA tag · immutable digest",
        )

        with Cluster("umc-infra  (public)", graph_attr=ca):
            bump = Git("deploy/<env>/<tag>-<digest>\nimage.tag + image.digest")
            bump_pr = Github("image bump PR")
            checks = GithubActions("Static validation\nrequired · strict")
            repo_infra = Github("컷오버 후 origin/main\nPR only · squash\nadmin bypass 없음")

        with Cluster("IDC 물리 서버 1대  ·  k3s", graph_attr=ca):
            argo = Argocd("ArgoCD")
            with Cluster("ns app", graph_attr=ca):
                prod = Deployment("umc-product-server\nprofile: prod")
            with Cluster("ns dev-app", graph_attr=ca):
                dev_app = Deployment("umc-product-server\nprofile: dev")

        dev >> Edge(label="push", fontcolor=theme["fg"]) >> repo_be >> Edge(
            label="트리거", fontcolor=theme["fg"]
        ) >> actions

        actions >> Edge(label="① 이미지 빌드 → push", fontcolor=theme["fg"]) >> registry
        actions >> Edge(label="② 배포 브랜치 생성", style="dashed",
                        fontcolor=theme["fg"]) >> bump
        bump >> Edge(label="③ PR 생성", fontcolor=theme["fg"]) >> bump_pr
        bump_pr >> Edge(label="④ 필수 검증", fontcolor=theme["fg"]) >> checks
        checks >> Edge(label="⑤ squash auto-merge", fontcolor=theme["fg"]) >> repo_infra

        # 이 화살표의 방향이 이 설계의 전부다 — 클러스터가 GitHub 을 읽는다
        argo >> Edge(label="⑥ 3분마다 읽음 (pull)", color=PULL, fontcolor=PULL) >> repo_infra

        argo >> Edge(label="values-prod.yaml → Production", fontcolor=theme["fg"]) >> prod
        argo >> Edge(label="values-dev.yaml → Development", fontcolor=theme["fg"]) >> dev_app

        prod >> Edge(label="⑦ anonymous digest pull", style="dotted",
                     fontcolor=theme["fg"]) >> registry
        dev_app >> Edge(style="dotted") >> registry


if __name__ == "__main__":
    build(THEME)
    print("out/cicd-flow.png 생성")

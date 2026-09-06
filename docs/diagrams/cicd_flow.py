"""CI/CD 배포 흐름 — 코드 push 부터 클러스터 반영까지.

매니페스트만으로는 그릴 수 없다. "Actions가 GHCR에 밀고 infra tag+digest를 갱신한 뒤
Helm 검증을 통과하면 main에 직접 push하고 Argo CD가 감지한다"는 어떤 매니페스트에도 적혀
있지 않기 때문이다. 그래서 손으로 그린다.

이 그림이 반드시 전달해야 하는 두 가지:
  1) ArgoCD → GitHub 방향이 pull 이다 (push 아님)
  2) 앱 레포에서 클러스터로 직접 apply하는 화살표가 없다

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
            bump = Git("values-<env>.yaml 갱신\nimage.tag + image.digest")
            checks = GithubActions("사전 Helm 검증\nlint --strict · template")
            repo_infra = Github("origin/main\nbot direct push\nserial · non-force")

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
        actions >> Edge(label="② values 갱신", style="dashed",
                        fontcolor=theme["fg"]) >> bump
        bump >> Edge(label="③ 사전 검증", fontcolor=theme["fg"]) >> checks
        checks >> Edge(label="④ commit → direct push", fontcolor=theme["fg"]) >> repo_infra

        # 이 화살표의 방향이 이 설계의 전부다 — 클러스터가 GitHub 을 읽는다
        argo >> Edge(label="⑤ 3분마다 읽음 (pull)", color=PULL, fontcolor=PULL) >> repo_infra

        argo >> Edge(label="values-prod.yaml → Production", fontcolor=theme["fg"]) >> prod
        argo >> Edge(label="values-dev.yaml → Development", fontcolor=theme["fg"]) >> dev_app

        prod >> Edge(label="⑥ anonymous digest pull", style="dotted",
                     fontcolor=theme["fg"]) >> registry
        dev_app >> Edge(style="dotted") >> registry


if __name__ == "__main__":
    build(THEME)
    print("out/cicd-flow.png 생성")

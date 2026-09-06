"""브랜치 하나가 올라가는 경로 — 기능 브랜치부터 prod 까지, 그리고 되돌리기.

cicd_flow.py 는 "push 하면 어떻게 배포되나"를 그린다. 이 그림은 그 앞단이다:
기능 브랜치를 따서 develop을 거쳐 prod까지 올리는 순서와, 잘못됐을 때 되돌리는 두 경로.

앱 승격은 사람이 결정하고, 각 환경의 infra tag+digest 변경은 사전 Helm 검증 후
main에 직접 push한다. dev/prod 갱신은 직렬화하며 force push하지 않는다.

    python branch_flow.py   →  out/branch-flow.png
"""

from diagrams import Cluster, Diagram, Edge
from diagrams.k8s.compute import Deployment
from diagrams.onprem.ci import GithubActions
from diagrams.onprem.client import User
from diagrams.onprem.gitops import Argocd
from diagrams.onprem.vcs import Git, Github

from theme import THEME, cluster_attr, edge_attr, graph_attr, node_attr

MANUAL = "#D68910"   # 사람이 눌러야 하는 것
AUTO = "#4C8FD0"     # 자동
BACK = "#E5534B"     # 되돌리기


def build(theme: dict) -> None:
    ca = cluster_attr(theme)
    with Diagram(
        "브랜치 하나가 올라가는 길  (주황 = 사람이 누름 · 파랑 = 자동 · 빨강 = 되돌리기)",
        filename="out/branch-flow",
        outformat="png",
        show=False,
        graph_attr=graph_attr(theme, rankdir="LR", ranksep="0.8", nodesep="0.5"),
        node_attr=node_attr(theme),
        edge_attr=edge_attr(theme),
    ):
        me = User("maintainer")

        with Cluster("umc-product-server", graph_attr=ca):
            feat = Git("feat/xxx\n기능 브랜치")
            dev_br = Github("develop")
            main_br = Github("main")

        cd_dev = GithubActions("trusted publish CI\ndev run")
        cd_prod = GithubActions("trusted publish CI\nprod run")

        with Cluster("umc-infra", graph_attr=ca):
            update_dev = Git("values-dev.yaml 갱신\nSHA tag + digest")
            update_prod = Git("values-prod.yaml 갱신\nSHA tag + digest")
            checks = GithubActions("사전 Helm 검증\nlint --strict · template")
            infra_main = Github("origin/main\nbot direct push\nserial · non-force")
            rollback_values = Git("known-good tag + digest\nvalues 복구")

        argo = Argocd("ArgoCD\nself-heal")

        with Cluster("k3s", graph_attr=ca):
            with Cluster("dev-app  검증", graph_attr=ca):
                dev_pod = Deployment("umc-product-server\ndev")
            with Cluster("app  운영", graph_attr=ca):
                prod_pod = Deployment("umc-product-server\nprod")

        # 올리는 길 — 앱 승격은 사람, infra values 갱신부터는 자동이다.
        me >> Edge(label="① 브랜치 따서 작업", color=MANUAL, fontcolor=MANUAL) >> feat
        feat >> Edge(label="② PR → Squash 머지", color=MANUAL, fontcolor=MANUAL) >> dev_br

        dev_br >> Edge(label="자동 트리거", color=AUTO, fontcolor=AUTO) >> cd_dev
        cd_dev >> Edge(label="SHA image + values 갱신", color=AUTO, fontcolor=AUTO) >> update_dev
        update_dev >> Edge(label="Helm lint + template", color=AUTO, fontcolor=AUTO) >> checks

        dev_pod >> Edge(label="③ dev 에서 확인", color=MANUAL, fontcolor=MANUAL, style="dotted") >> me
        dev_br >> Edge(label="④ PR → Merge commit\n(장수 브랜치라 squash 금지)",
                       color=MANUAL, fontcolor=MANUAL) >> main_br

        main_br >> Edge(label="자동 트리거", color=AUTO, fontcolor=AUTO) >> cd_prod
        cd_prod >> Edge(label="SHA image + values 갱신", color=AUTO, fontcolor=AUTO) >> update_prod
        update_prod >> Edge(label="Helm lint + template", color=AUTO, fontcolor=AUTO) >> checks
        checks >> Edge(label="commit → direct push", color=AUTO, fontcolor=AUTO) >> infra_main
        argo >> Edge(label="infra main pull", color=AUTO, fontcolor=AUTO,
                     style="dashed") >> infra_main
        argo >> Edge(label="values-dev", color=AUTO, fontcolor=AUTO) >> dev_pod
        argo >> Edge(label="values-prod", color=AUTO, fontcolor=AUTO) >> prod_pod

        # 되돌리는 길 — 즉시 Argo CD rollback과 Git 기준값 복구
        prod_pod >> Edge(label="문제 발견", color=BACK, fontcolor=BACK, style="dotted") >> me
        me >> Edge(label="ⓐ ArgoCD UI 에서 이전 버전 선택\n(빠름 · Git 은 그대로)",
                   color=BACK, fontcolor=BACK) >> argo
        me >> Edge(label="ⓑ known-good tag+digest로 values 복구\n(Git 이 정답지로 남음)",
                   color=BACK, fontcolor=BACK) >> rollback_values
        rollback_values >> Edge(label="사전 Helm 검증", color=BACK, fontcolor=BACK) >> checks


if __name__ == "__main__":
    build(THEME)
    print("out/branch-flow.png 생성")

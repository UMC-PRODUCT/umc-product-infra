"""PR 프리뷰 수명주기 — 생성·검증·정리와 공유 자원만 표시한다.

AWS 인증과 ESO 내부 동작은 secret_supply_chain.py로 분리한다. 이 그림에서 중요한
보안 전제는 preview가 신뢰된 내부 PR만 받으며 모든 PR이 같은 자격증명을 공유한다는 점이다.

    python preview_env.py   →  out/preview-env.png
"""

from diagrams import Cluster, Diagram, Edge
from diagrams.k8s.clusterconfig import Quota
from diagrams.k8s.compute import Deployment, Job, StatefulSet
from diagrams.k8s.network import Ingress, Service
from diagrams.k8s.podconfig import Secret
from diagrams.onprem.ci import GithubActions
from diagrams.onprem.client import User
from diagrams.onprem.container import Docker
from diagrams.onprem.gitops import Argocd
from diagrams.onprem.vcs import Github

from theme import THEME, cluster_attr, edge_attr, graph_attr, node_attr

MANUAL = "#D68910"
AUTO = "#4C8FD0"
DELETE = "#8B95A1"
RISK = "#E5534B"


def build(theme: dict) -> None:
    ca = cluster_attr(theme)
    fg = theme["fg"]

    with Diagram(
        "PR 프리뷰 수명주기 — 신뢰된 내부 PR만 지원",
        filename="out/preview-env",
        outformat="png",
        show=False,
        graph_attr=graph_attr(theme, rankdir="LR", ranksep="1.1", nodesep="0.6"),
        node_attr=node_attr(theme),
        edge_attr=edge_attr(theme),
    ):
        developer = User("개발자", fontcolor=fg)

        with Cluster("umc-product-server", graph_attr=ca):
            pr = Github("same-repo trusted PR #42\ndevelop 대상 · preview 라벨", fontcolor=fg)
            build = GithubActions("publish-preview CI\nauthor + head repo 검증\n라벨 시 admit: 최대 2", fontcolor=fg)

        image = Docker(
            "public GHCR\numc-product/umc-product-server\n:<PR head SHA 12자리>",
            fontcolor=fg,
        )

        release = Github(
            "umc-infra main\npreview-releases/pr-42.json\n마지막 성공 SHA · digest",
            fontcolor=fg,
        )

        with Cluster("선행 설치 · PR마다 반복하지 않음", graph_attr=ca):
            appset = Argocd(
                "ApplicationSet\nPR label + targetBranch 필터\n× 성공 기록 JSON · 120초 polling",
                fontcolor=fg,
            )
            shared_secrets = Secret(
                "ESO-managed shared secrets\nargocd: GitHub token\npreview: app · PostgreSQL",
                fontcolor=fg,
            )

        with Cluster("ns argocd · PR마다 생성·삭제", graph_attr=ca):
            application = Argocd("Application\numc-product-preview-42", fontcolor=fg)

        with Cluster("k3s · namespace preview", graph_attr=ca):
            quota = Quota(
                "preview-budget\nDeployment 최대 2 · Service 최대 4\nCertificate 최대 2 (wildcard · Mailpit loopback)",
                fontcolor=fg,
            )
            certificate = Secret(
                "공용 wildcard Certificate\n*.preview.university.neordinary.com\nPR 삭제 후에도 유지",
                fontcolor=fg,
            )
            with Cluster("PR별 리소스 · ApplicationSet이 Deployment/Ingress 활성화", graph_attr=ca):
                service = Service("umc-product-server-pr-42\nService quota로 자리 확보", fontcolor=fg)
                ingress = Ingress(
                    "api-pr-42.preview.university.neordinary.com\nExternalDNS exact record",
                    fontcolor=fg,
                )
                app = Deployment(
                    "umc-product-server-pr-42\n2Gi request · 3Gi limit\npreview-lowest · Mailpit sidecar",
                    fontcolor=fg,
                )
                create_db = Job("createdb Job\numc_product_pr42 · dev 복사", fontcolor=fg)
                delete_db = Job("PostDelete dropdb\nexact umc_product_pr42", fontcolor=fg)

            postgres = StatefulSet(
                "postgres-preview\n모든 PR이 공유 · DB만 분리",
                fontcolor=fg,
            )

        warning = Secret(
            "격리 경계\nshared Secret + DB role\n외부·fork PR 금지",
            fontcolor=fg,
        )

        developer >> Edge(
            label="① PR + preview 라벨",
            color=MANUAL,
            fontcolor=MANUAL,
        ) >> pr
        pr >> Edge(label="② trusted build", color=AUTO, fontcolor=AUTO) >> build
        build >> Edge(label="PR SHA tag 게시", color=AUTO, fontcolor=AUTO) >> image
        build >> Edge(label="익명 pull 검증 후 기록", color=AUTO, fontcolor=AUTO) >> release

        appset >> Edge(
            label="③ PR 감지",
            color=AUTO,
            fontcolor=AUTO,
            style="dashed",
        ) >> pr
        appset >> Edge(
            label="성공 기록 조회",
            color=AUTO,
            fontcolor=AUTO,
            style="dashed",
        ) >> release
        appset >> Edge(label="④ 기록 있는 PR만\nApplication 생성", color=AUTO, fontcolor=AUTO) >> application

        application >> Edge(label="wave -2", color=AUTO, fontcolor=AUTO) >> service
        certificate >> Edge(label="preview-wildcard-tls", color=AUTO, fontcolor=AUTO) >> ingress
        application >> Edge(label="wave 1", color=AUTO, fontcolor=AUTO) >> ingress
        application >> Edge(label="wave 0", color=AUTO, fontcolor=AUTO) >> app
        application >> Edge(label="wave -1", color=AUTO, fontcolor=AUTO) >> create_db
        quota >> Edge(
            label="admission 제한",
            color=RISK,
            fontcolor=RISK,
            style="dashed",
        ) >> app
        quota >> Edge(color=RISK, style="dashed") >> service
        quota >> Edge(color=RISK, style="dashed") >> certificate
        app >> Edge(
            label="anonymous image pull",
            color=AUTO,
            fontcolor=AUTO,
            style="dotted",
        ) >> image

        shared_secrets >> Edge(
            label="preview-github-token",
            style="dotted",
            fontcolor=fg,
        ) >> appset
        shared_secrets >> Edge(
            label="envFrom",
            style="dotted",
            fontcolor=fg,
        ) >> app
        shared_secrets >> Edge(
            label="POSTGRES_*",
            style="dotted",
            fontcolor=fg,
        ) >> postgres

        create_db >> Edge(label="멱등 생성", color=AUTO, fontcolor=AUTO) >> postgres
        app >> Edge(
            label="jdbc …/umc_product_pr42\nFlyway",
            color=AUTO,
            fontcolor=AUTO,
        ) >> postgres
        developer >> Edge(
            label="⑤ 모바일 직접 호출\nHTTPS · 앱 API 인증",
            color=MANUAL,
            fontcolor=MANUAL,
            style="dotted",
            constraint="false",
        ) >> ingress

        developer >> Edge(
            label="⑥ PR 닫기/라벨 제거\nApplication과 PR 리소스 삭제",
            color=DELETE,
            fontcolor=DELETE,
            style="dashed",
        ) >> pr
        application >> Edge(
            label="삭제 후 hook",
            color=DELETE,
            fontcolor=DELETE,
            style="dashed",
        ) >> delete_db
        delete_db >> Edge(
            label="정확한 DB만 DROP\n실패 시 보존",
            color=DELETE,
            fontcolor=DELETE,
            style="dashed",
        ) >> postgres
        warning >> Edge(
            label="공유 credential",
            color=RISK,
            fontcolor=RISK,
            style="dotted",
        ) >> shared_secrets


if __name__ == "__main__":
    build(THEME)
    print("out/preview-env.png 생성")

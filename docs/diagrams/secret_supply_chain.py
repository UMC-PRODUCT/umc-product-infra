"""IDC k3s의 AWS Secrets Manager → Kubernetes Secret 공급망.

이 그림은 secret-zero, STS 신뢰 경계, GitOps 계약, 런타임 Secret 생성 책임을 분리한다.
실제 비밀값은 그림과 Git 어디에도 없다.

    python secret_supply_chain.py
      → out/secret-supply-chain.png
"""

from diagrams import Cluster, Diagram, Edge
from diagrams.aws.security import IAM, IAMAWSSts, IAMRole, SecretsManager
from diagrams.k8s.compute import Deployment
from diagrams.k8s.ecosystem import Helm
from diagrams.k8s.others import CRD
from diagrams.k8s.podconfig import Secret
from diagrams.onprem.gitops import Argocd
from diagrams.onprem.iac import Ansible

from theme import THEME, cluster_attr, edge_attr, graph_attr, node_attr

BOOTSTRAP = "#D68910"
AWS = "#E5534B"
GITOPS = "#4C8FD0"
RUNTIME = "#2E8B57"


def build(theme: dict) -> None:
    ca = cluster_attr(theme)
    fg = theme["fg"]

    with Diagram(
        "Secret supply chain — IDC k3s · desired state",
        filename="out/secret-supply-chain",
        outformat="png",
        show=False,
        graph_attr=graph_attr(theme, rankdir="LR", ranksep="1.2", nodesep="0.6"),
        node_attr=node_attr(theme),
        edge_attr=edge_attr(theme),
    ):
        with Cluster("Bootstrap · Git 밖", graph_attr=ca):
            ansible = Ansible(
                "Ansible controller\nenv/prompt로 key 입력\nno_log · 저장소 미보관",
                fontcolor=fg,
            )
            bootstrap_secret = Secret(
                "external-secrets/aws-bootstrap\nSecret at-rest encryption",
                fontcolor=fg,
            )
            bootstrap_risk = IAM(
                "bootstrap IAM User\n장기 key: 1차 운영 tradeoff\nAssumeRole·TagSession만",
                fontcolor=fg,
            )

        with Cluster("GitOps contract · 값 없음", graph_attr=ca):
            secrets_app = Argocd("secrets Application\nwave -1", fontcolor=fg)
            stores = CRD(
                "namespaced SecretStore 9\n환경 role · prefix",
                fontcolor=fg,
            )
            external = CRD(
                "ExternalSecret 28\nexpected key/property 계약\nPeriodic 1h",
                fontcolor=fg,
            )

        with Cluster("AWS security boundary", graph_attr=ca):
            sts = IAMAWSSts(
                "STS AssumeRole\nnamespace/store session tags",
                fontcolor=fg,
            )
            roles = IAMRole(
                "환경별 IAM Role\nprod · dev · preview · monitoring\nargocd · cert-manager · external-dns",
                fontcolor=fg,
            )
            manager = SecretsManager(
                "AWS Secrets Manager\nJSON source 26\n환경 prefix로 격리",
                fontcolor=fg,
            )

        with Cluster("k3s runtime", graph_attr=ca):
            eso = Helm("External Secrets Operator\n2.10.0", fontcolor=fg)
            runtime_secret = Secret(
                "Kubernetes Secret\nOwner · remote-delete Retain",
                fontcolor=fg,
            )
            workloads = Deployment(
                "PostgreSQL · app · monitoring\n앱 DB role은 admin과 분리",
                fontcolor=fg,
            )

        ansible >> Edge(
            label="최초 설치·회전 시 stdin 주입",
            color=BOOTSTRAP,
            fontcolor=BOOTSTRAP,
        ) >> bootstrap_secret
        bootstrap_risk >> Edge(
            label="access key 발급",
            color=BOOTSTRAP,
            fontcolor=BOOTSTRAP,
            style="dotted",
        ) >> ansible
        bootstrap_secret >> Edge(
            label="AWS SDK credential",
            color=BOOTSTRAP,
            fontcolor=BOOTSTRAP,
        ) >> eso

        secrets_app >> Edge(label="sync", color=GITOPS, fontcolor=GITOPS) >> stores
        secrets_app >> Edge(label="sync", color=GITOPS, fontcolor=GITOPS) >> external
        external >> Edge(
            label="reconcile 대상",
            color=GITOPS,
            fontcolor=GITOPS,
            style="dotted",
        ) >> eso
        stores >> Edge(
            label="role ARN · prefix",
            color=GITOPS,
            fontcolor=GITOPS,
            style="dotted",
        ) >> eso

        eso >> Edge(label="AssumeRole", color=AWS, fontcolor=AWS) >> sts
        bootstrap_risk >> Edge(
            label="trust principal",
            color=AWS,
            fontcolor=AWS,
            style="dotted",
        ) >> sts
        sts >> Edge(label="temporary session", color=AWS, fontcolor=AWS) >> roles
        roles >> Edge(
            label="GetSecretValue",
            color=AWS,
            fontcolor=AWS,
        ) >> manager
        manager >> Edge(
            label="JSON properties",
            color=AWS,
            fontcolor=AWS,
            style="dashed",
        ) >> eso

        eso >> Edge(
            label="생성·갱신",
            color=RUNTIME,
            fontcolor=RUNTIME,
        ) >> runtime_secret
        runtime_secret >> Edge(
            label="secretKeyRef · envFrom · volume\nGHCR은 public anonymous pull",
            color=RUNTIME,
            fontcolor=RUNTIME,
        ) >> workloads


if __name__ == "__main__":
    build(THEME)
    print("out/secret-supply-chain.png 생성")

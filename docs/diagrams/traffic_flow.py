"""트래픽 흐름 — 누가 누구를 부르는가.

매니페스트에서 자동 생성하는 아키텍처 그림은 "무엇이 있는가"(인벤토리)를 보여준다.
하지만 아래 연결들은 k8s 참조가 아니라 값 안의 문자열이라 도구가 추론할 수 없다.

  app → postgres      DATABASE_URL 환경변수의 접속 문자열
  app → collector     OTEL_URL 환경변수
  collector → 백엔드  Application 안 valuesObject 의 exporter 설정

그래서 이 그림은 손으로 그린다. 두 그림은 대체 관계가 아니라 축이 다르다.

    python traffic_flow.py   →  out/traffic-flow.png
"""

from diagrams import Cluster, Diagram, Edge
from diagrams.aws.network import Route53
from diagrams.aws.storage import S3
from diagrams.k8s.compute import Cronjob, Deployment, StatefulSet
from diagrams.k8s.network import Ingress, Service
from diagrams.onprem.client import Users
from diagrams.onprem.logging import Loki
from diagrams.onprem.monitoring import Grafana, Prometheus
from diagrams.onprem.network import Internet, Traefik
from diagrams.onprem.tracing import Tempo

from theme import THEME, cluster_attr, edge_attr, graph_attr, node_attr

DATA = "#4C8FD0"   # 사용자 요청 경로
TELEM = "#A175D8"  # 관측 데이터 (push)
OPS = "#E5534B"    # 운영 접근


def build(theme: dict) -> None:
    ca = cluster_attr(theme)

    with Diagram(
        "UMC Product 트래픽 흐름  (파랑 요청 · 보라 관측 · 빨강 운영 접근)",
        filename="out/traffic-flow",
        outformat="png",
        show=False,
        graph_attr=graph_attr(theme, rankdir="LR"),
        node_attr=node_attr(theme),
        edge_attr=edge_attr(theme),
    ):
        public = Users("모바일 · API 사용자")
        dns = Route53(
            "Route 53 authoritative DNS\nuniversity.neordinary.com child zone\nexact A/TXT · proxy 없음",
        )
        outside = Internet("외부 uptime monitor\nHTTPS + heartbeat")
        backup_store = S3("전용 AWS S3 backup\nSSE-S3 · versioning · 35d\nwriter: put only")
        team = Users("UMC 팀원\nGrafana Viewer")
        operator = Users("인프라 관리자")
        management = Internet("공인 SSH :22\n개인 계정·공개키 · local tunnel")

        with Cluster("ns kube-system", graph_attr=ca):
            traefik = Traefik("traefik websecure\n443 only · public 80 닫힘\nUFW: direct HTTPS 허용")

        with Cluster("ns cert-manager · external-dns", graph_attr=ca):
            dns_tls = Deployment(
                "cert-manager · ExternalDNS\nLet's Encrypt DNS-01\nexact Ingress record"
            )

        with Cluster("ns app  ·  prod", graph_attr=ca):
            ing = Ingress("api.university.neordinary.com\npublic API · 앱 인증")
            svc = Service("umc-product-server\n:80")
            app = Deployment("umc-product-server\n8080")

        with Cluster("ns db  ·  prod", graph_attr=ca):
            pg = StatefulSet("PostgreSQL 18 + PostGIS 3.6\nDB umc_product")
            backup = Cronjob("pg-backup\n03:00 Asia/Seoul\nstart deadline 1h · runtime ≤2h\nsuspend:true")

        with Cluster("ns dev-app  ·  dev", graph_attr=ca):
            ing_d = Ingress("api-dev.university.neordinary.com\npublic API · 앱 인증")
            app_d = Deployment("umc-product-server")

        with Cluster("ns dev-db  ·  dev", graph_attr=ca):
            pg_d = StatefulSet("PostgreSQL 18 + PostGIS 3.6\nDB umc_product_dev")

        with Cluster("ns preview  ·  최대 3", graph_attr=ca):
            ing_p = Ingress("api-pr-<PR>.university.neordinary.com\npublic API · 앱 인증")
            app_p = Deployment("umc-product-server-pr-<PR>")
            pg_p = StatefulSet("postgres-preview\nPR별 database")

        with Cluster("ns monitoring  ·  default-deny ingress", graph_attr=ca):
            graf_ing = Ingress(
                "grafana.university.neordinary.com\npublic HTTPS · Grafana login"
            )
            collector = Prometheus("otel-collector :4317/4318\n1Gi cap · limiter\ntraces 100%")
            tempo = Tempo("tempo\n7d · PVC 50Gi · 2Gi cap")
            loki = Loki("loki")
            prom = Prometheus("prometheus + alertmanager\n실제 scrape target만\n같은-node 소실은 감지 못함")
            graf = Grafana("grafana\n익명·회원가입 off\n팀원별 Viewer 계정")

        # 사용자 요청 경로
        public >> Edge(color=DATA, fontcolor=DATA, style="dotted",
                       label="DNS 조회") >> dns
        public >> Edge(color=DATA, fontcolor=DATA,
                       label="direct HTTPS\n고정 public IPv4:443") >> traefik
        traefik >> Edge(color=DATA, fontcolor=DATA, style="dashed",
                        label="컷오버 후\nHost 매칭") >> ing >> Edge(color=DATA, style="dashed") >> svc
        svc >> Edge(color=DATA) >> app
        app >> Edge(color=DATA, fontcolor=DATA, label="jdbc  postgres.db.svc") >> pg

        traefik >> Edge(color=DATA, style="dashed") >> ing_d >> Edge(color=DATA, style="dashed") >> app_d
        app_d >> Edge(color=DATA, fontcolor=DATA, style="dashed",
                      label="jdbc  postgres.dev-db.svc") >> pg_d
        traefik >> Edge(color=DATA, style="dashed") >> ing_p >> Edge(color=DATA, style="dashed") >> app_p
        app_p >> Edge(color=DATA, fontcolor=DATA, style="dashed",
                      label="jdbc  PR database") >> pg_p

        dns_tls >> Edge(
            color=DATA,
            fontcolor=DATA,
            style="dotted",
            label="분리된 AWS 자격증명\nDNS-01 + exact A/TXT",
        ) >> dns
        dns_tls >> Edge(color=DATA, style="dotted", label="TLS Secret") >> ing
        dns_tls >> Edge(color=DATA, style="dotted") >> ing_d
        dns_tls >> Edge(color=DATA, style="dotted") >> ing_p
        dns_tls >> Edge(color=OPS, style="dotted") >> graf_ing

        # restore 리허설을 통과한 뒤에만 suspend를 해제하는 운영 backup 경로.
        backup >> Edge(color=OPS, fontcolor=OPS, style="dashed",
                       label="restore gate 후\npg_dump + sha256") >> backup_store

        # 관측 — 앱이 밀어 넣는다 (scrape 아님)
        app >> Edge(color=TELEM, fontcolor=TELEM,
                    label="NetworkPolicy 허용\nOTLP push") >> collector
        app_d >> Edge(color=TELEM, fontcolor=TELEM, style="dashed",
                      label="NetworkPolicy 허용") >> collector
        app_p >> Edge(color=TELEM, fontcolor=TELEM, style="dashed") >> collector
        collector >> Edge(color=TELEM, fontcolor=TELEM, label="traces") >> tempo
        collector >> Edge(color=TELEM, fontcolor=TELEM, label="logs") >> loki
        collector >> Edge(color=TELEM, fontcolor=TELEM, label="metrics") >> prom
        graf << Edge(color=TELEM, fontcolor=TELEM, style="dotted",
                     label="데이터소스") << tempo
        graf << Edge(color=TELEM, style="dotted") << loki
        graf << Edge(color=TELEM, style="dotted") << prom

        # 팀원은 public HTTPS에서 각자의 Viewer 계정으로 Grafana를 본다.
        team >> Edge(color=OPS, fontcolor=OPS, style="dotted",
                     label="DNS 조회") >> dns
        team >> Edge(color=OPS, fontcolor=OPS, style="dotted",
                     label="HTTPS + login") >> traefik
        traefik >> Edge(color=OPS, style="dotted") >> graf_ing >> Edge(
            color=OPS, style="dotted"
        ) >> graf

        # 개인 관리자 SSH로 local health를 점검한다. SSH 장애 복구는 제공업체 console을 쓴다.
        operator >> Edge(color=OPS, fontcolor=OPS, style="dotted",
                         label="개인 SSH key") >> management
        management >> Edge(color=OPS, fontcolor=OPS, style="dotted",
                           label="SSH local port-forward\nhealth · 복구") >> graf

        outside >> Edge(color=OPS, fontcolor=OPS, style="dotted",
                        label="노드 밖에서 direct HTTPS 감시") >> traefik


if __name__ == "__main__":
    build(THEME)
    print("out/traffic-flow.png 생성")

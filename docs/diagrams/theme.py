"""다이어그램의 공용 테마."""

# 모든 구조 그림이 공유하는 색상이다. 개별 흐름을 나타내는 선 색상은 각 그림이 따로 정한다.
THEME = {
    "bg": "#FFFFFF",
    "fg": "#1F2328",
    "cluster_bg": "#EEF3F8",
    "cluster_fg": "#48525C",
}


def graph_attr(theme: dict, **extra) -> dict:
    """Diagram 전체 속성."""
    base = {
        "fontname": "Helvetica",
        "bgcolor": theme["bg"],
        "fontcolor": theme["fg"],
        "pad": "0.5",
        "splines": "spline",
        "nodesep": "0.5",
        "ranksep": "1.1",
    }
    # 공통 기본값 위에 그림별 방향·간격을 덮어써서 파일마다 테마 전체를 복제하지 않는다.
    base.update(extra)
    return base


# 리소스 아이콘 아래 이름의 서식이다. 연결선 설명은 edge_attr에서 별도로 조절한다.
def node_attr(theme: dict) -> dict:
    return {"fontname": "Helvetica", "fontcolor": theme["fg"], "fontsize": "12"}


# 선 위 설명의 서식만 공유한다. 화살표의 방향·색·의미는 각 그림의 Edge가 결정한다.
def edge_attr(theme: dict) -> dict:
    return {"fontname": "Helvetica", "fontcolor": theme["fg"], "fontsize": "11"}


def cluster_attr(theme: dict) -> dict:
    """Cluster 박스 속성 — 배경과 제목 글자색을 함께 지정해야 한다."""
    return {
        "bgcolor": theme["cluster_bg"],
        "fontcolor": theme["cluster_fg"],
        "fontname": "Helvetica",
        "fontsize": "13",
        "penwidth": "1",
        "pencolor": theme["cluster_fg"],
        "style": "rounded",
    }

"""다이어그램의 공용 테마."""

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
    base.update(extra)
    return base


def node_attr(theme: dict) -> dict:
    return {"fontname": "Helvetica", "fontcolor": theme["fg"], "fontsize": "12"}


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

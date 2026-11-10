"""Tiện ích tuần tự hóa đồ thị NetworkX <-> dict (JSON) để truyền qua State và lưu SQLite."""
import networkx as nx


def graph_to_dict(G: nx.MultiDiGraph) -> dict:
    try:
        return nx.node_link_data(G, edges='edges')   # NetworkX >= 3.4
    except TypeError:
        return nx.node_link_data(G)                  # phiên bản cũ: khóa 'links'


def graph_from_dict(data: dict) -> nx.MultiDiGraph:
    try:
        return nx.node_link_graph(data, edges='edges')
    except TypeError:
        return nx.node_link_graph(data)


def edges_of(data: dict) -> list:
    return data.get('edges') or data.get('links', [])


def nodes_of(data: dict) -> list:
    return data.get('nodes', [])

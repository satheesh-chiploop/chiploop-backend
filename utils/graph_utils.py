# backend/utils/graph_utils.py
import networkx as nx

def build_capability_graph(agent_caps):
    G = nx.DiGraph()
    for agent, meta in agent_caps.items():
        G.add_node(agent, domain=meta["domain"])
        for other, m2 in agent_caps.items():
            if set(meta["outputs"]) & set(m2["inputs"]):
                G.add_edge(agent, other)
    return G

def serialize_graph(G):
    return {
        "nodes": [{"id": n, "domain": G.nodes[n]["domain"]} for n in G.nodes],
        "edges": [{"source": u, "target": v} for u, v in G.edges],
    }

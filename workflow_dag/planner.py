from typing import Any, Dict, List

from .models import DAGEdge, DAGNode, WorkflowDAG
from .validator import _topological_groups


def dag_from_agents(
    agents: List[str],
    *,
    loop_type: str = "digital",
    metadata: Dict[str, Any] | None = None,
    infer_parallel: bool = False,
) -> WorkflowDAG:
    nodes = []
    edges = []
    dependencies = _infer_dependencies(agents) if infer_parallel else {}
    previous_id = ""
    for index, agent_name in enumerate(agents):
        node_id = f"n{index + 1}"
        depends_on = dependencies.get(node_id, []) if infer_parallel else ([previous_id] if previous_id else [])
        nodes.append(
            DAGNode(
                node_id=node_id,
                agent_name=agent_name,
                loop_type=loop_type,
                depends_on=depends_on,
                can_run_parallel=bool(infer_parallel or index > 0),
            )
        )
        for dep in depends_on:
            edges.append(DAGEdge(from_node=dep, to_node=node_id))
        if previous_id and not infer_parallel and not depends_on:
            edges.append(DAGEdge(from_node=previous_id, to_node=node_id))
        previous_id = node_id
    return WorkflowDAG(nodes=nodes, edges=edges, metadata=metadata or {"planner_mode": "serial_order_as_dag"})


def dag_from_studio_graph(graph: Dict[str, Any], *, loop_type: str = "digital") -> WorkflowDAG:
    nodes = []
    edges = [DAGEdge.from_dict(edge) for edge in graph.get("edges", [])]
    dependencies = {edge.to_node: [] for edge in edges}
    for edge in edges:
        dependencies.setdefault(edge.to_node, []).append(edge.from_node)

    for item in graph.get("nodes", []):
        data = item.get("data") or {}
        node_id = str(item.get("id") or f"n{len(nodes) + 1}")
        agent_name = data.get("backendLabel") or data.get("uiLabel") or item.get("type") or item.get("label") or ""
        nodes.append(
            DAGNode(
                node_id=node_id,
                agent_name=str(agent_name),
                loop_type=loop_type,
                depends_on=dependencies.get(node_id, []),
                can_run_parallel=True,
            )
        )
    return WorkflowDAG(nodes=nodes, edges=edges, metadata={"source": "studio_graph"})


def parallel_groups(dag: WorkflowDAG) -> List[List[str]]:
    return _topological_groups(dag)


def dry_run_plan(dag: WorkflowDAG) -> Dict[str, Any]:
    groups = parallel_groups(dag)
    node_by_id = {node.node_id: node for node in dag.nodes}
    return {
        "workflow_mode": "dag",
        "dry_run": True,
        "execution_order": [node_id for group in groups for node_id in group],
        "parallel_groups": groups,
        "dependency_graph": {
            node.node_id: {
                "agent_name": node.agent_name,
                "depends_on": list(node.depends_on),
                "can_run_parallel": node.can_run_parallel,
            }
            for node in dag.nodes
        },
        "parallel_group_agents": [
            [node_by_id[node_id].agent_name for node_id in group]
            for group in groups
        ],
    }


def _infer_dependencies(agents: List[str]) -> Dict[str, List[str]]:
    deps: Dict[str, List[str]] = {}
    last_anchor = ""
    branch_nodes: List[str] = []

    for index, agent_name in enumerate(agents):
        node_id = f"n{index + 1}"
        name = agent_name.lower()

        if index == 0:
            deps[node_id] = []
            last_anchor = node_id
            continue

        is_branch = any(token in name for token in ("sim", "firmware", "software", "testbench", "coverage"))
        is_final = any(token in name for token in ("integration", "handoff", "summary", "package", "top assembly"))
        is_rtl_anchor = "rtl" in name or "architecture" in name or "spec" in name

        if is_branch and last_anchor:
            deps[node_id] = [last_anchor]
            branch_nodes.append(node_id)
        elif is_final and branch_nodes:
            deps[node_id] = list(branch_nodes)
            last_anchor = node_id
            branch_nodes = []
        else:
            deps[node_id] = [f"n{index}"]
            if is_rtl_anchor or not branch_nodes:
                last_anchor = node_id

    return deps

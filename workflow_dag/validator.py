from typing import Dict, List, Set, Tuple

from studio_contract.registry import load_registry

from .models import WorkflowDAG


def _node_map(dag: WorkflowDAG) -> Dict[str, object]:
    return {node.node_id: node for node in dag.nodes}


def _detect_cycle(dag: WorkflowDAG) -> bool:
    graph = {node.node_id: [] for node in dag.nodes}
    for edge in dag.edges:
        graph.setdefault(edge.from_node, []).append(edge.to_node)

    visiting: Set[str] = set()
    visited: Set[str] = set()

    def visit(node_id: str) -> bool:
        if node_id in visiting:
            return True
        if node_id in visited:
            return False
        visiting.add(node_id)
        for child in graph.get(node_id, []):
            if visit(child):
                return True
        visiting.remove(node_id)
        visited.add(node_id)
        return False

    return any(visit(node_id) for node_id in graph)


def validate_dag(dag: WorkflowDAG, registry_dir: str = "registry") -> Tuple[bool, List[str]]:
    errors: List[str] = []
    nodes = _node_map(dag)

    if not dag.nodes:
        errors.append("dag has no nodes")

    if len(nodes) != len(dag.nodes):
        errors.append("dag contains duplicate node_id values")

    registry = load_registry(registry_dir)
    for node in dag.nodes:
        if not node.node_id:
            errors.append("node missing node_id")
        if not node.agent_name:
            errors.append(f"node {node.node_id}: missing agent_name")
        if node.agent_name and node.agent_name not in registry.agents:
            errors.append(f"node {node.node_id}: missing registered agent {node.agent_name}")
        for dep in node.depends_on:
            if dep not in nodes:
                errors.append(f"node {node.node_id}: missing dependency {dep}")

    edge_pairs = {(edge.from_node, edge.to_node) for edge in dag.edges}
    for edge in dag.edges:
        if edge.from_node not in nodes:
            errors.append(f"edge references missing from_node {edge.from_node}")
        if edge.to_node not in nodes:
            errors.append(f"edge references missing to_node {edge.to_node}")
        if edge.from_node == edge.to_node:
            errors.append(f"edge self-cycle {edge.from_node}")

    if len(dag.nodes) > 1:
        connected = {edge.from_node for edge in dag.edges} | {edge.to_node for edge in dag.edges}
        for node in dag.nodes:
            if node.node_id not in connected:
                errors.append(f"node {node.node_id}: orphan node")

    for node in dag.nodes:
        for dep in node.depends_on:
            if (dep, node.node_id) not in edge_pairs:
                errors.append(f"node {node.node_id}: depends_on {dep} missing matching edge")

    if _detect_cycle(dag):
        errors.append("dag contains a cycle")

    produced = set()
    initial_inputs = set(dag.metadata.get("available_inputs") or [])
    for group in _topological_groups(dag):
        for node_id in group:
            node = nodes[node_id]
            missing = [item for item in node.required_inputs if item not in initial_inputs and item not in produced]
            if missing:
                errors.append(f"node {node.node_id}: unsatisfied required_inputs {missing}")
        for node_id in group:
            produced.update(nodes[node_id].produced_outputs)

    return not errors, errors


def _topological_groups(dag: WorkflowDAG) -> List[List[str]]:
    nodes = _node_map(dag)
    deps = {node.node_id: set(node.depends_on) for node in dag.nodes}
    for edge in dag.edges:
        deps.setdefault(edge.to_node, set()).add(edge.from_node)
    completed: Set[str] = set()
    groups: List[List[str]] = []
    while len(completed) < len(nodes):
        ready = sorted(node_id for node_id, need in deps.items() if node_id not in completed and need <= completed)
        if not ready:
            return groups
        groups.append(ready)
        completed.update(ready)
    return groups

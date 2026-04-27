import logging
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, Optional

from agents.runtime import AgentContext, execute_legacy_agent

from .models import DAGExecutionResult, DAGExecutionState, DAGNode, DAGNodeResult, WorkflowDAG
from .planner import parallel_groups
from .state_store import DAGStateStore
from .validator import validate_dag


logger = logging.getLogger("chiploop.workflow_dag")
AgentMap = Dict[str, Callable[[dict], dict]]


def _run_node(node: DAGNode, agent_fn: Callable[[dict], dict], state: Dict[str, Any]) -> DAGNodeResult:
    context = AgentContext.from_state(state, node.agent_name)
    result = execute_legacy_agent(agent_fn, context)
    updates = result.to_state_update()
    artifacts = updates.get("artifacts") if isinstance(updates.get("artifacts"), dict) else {}
    return DAGNodeResult(
        status=str(updates.get("status") or result.status),
        artifacts=artifacts,
        logs=[result.log] if result.log else [],
        state_updates=updates,
    )


def execute_dag(
    dag: WorkflowDAG,
    agent_map: AgentMap,
    *,
    initial_state: Optional[Dict[str, Any]] = None,
    max_workers: int = 4,
    dry_run: bool = False,
    registry_dir: str = "registry",
) -> DAGExecutionResult:
    start = time.time()
    ok, errors = validate_dag(dag, registry_dir=registry_dir)
    if not ok:
        raise ValueError("; ".join(errors))

    groups = parallel_groups(dag)
    if dry_run:
        return DAGExecutionResult(
            total_nodes=len(dag.nodes),
            completed_nodes=0,
            failed_nodes=0,
            skipped_nodes=0,
            execution_time=time.time() - start,
            dry_run=True,
            parallel_groups=groups,
        )

    state = DAGExecutionState(node_status={node.node_id: "pending" for node in dag.nodes})
    shared = DAGStateStore(initial_state)
    nodes = {node.node_id: node for node in dag.nodes}
    results: Dict[str, DAGNodeResult] = {}
    skipped = set()
    stop = False

    for group in groups:
        runnable = [node_id for node_id in group if node_id not in skipped]
        if not runnable or stop:
            for node_id in group:
                state.node_status[node_id] = "skipped"
                results.setdefault(node_id, DAGNodeResult(status="skipped", logs=["Skipped due to dependency failure."]))
            continue

        with ThreadPoolExecutor(max_workers=min(max_workers, max(1, len(runnable)))) as pool:
            futures = {}
            for node_id in runnable:
                node = nodes[node_id]
                agent_fn = agent_map.get(node.agent_name)
                if agent_fn is None:
                    state.node_status[node_id] = "failed"
                    state.errors[node_id] = f"missing agent implementation {node.agent_name}"
                    results[node_id] = DAGNodeResult(status="failed", logs=[state.errors[node_id]])
                    if node.failure_policy == "fail_fast":
                        stop = True
                    continue
                logger.info("dag.node.start", extra={"node_id": node_id, "agent_name": node.agent_name})
                state.node_status[node_id] = "running"
                futures[pool.submit(_run_node, node, agent_fn, shared.snapshot())] = node_id

            for future in as_completed(futures):
                node_id = futures[future]
                node = nodes[node_id]
                try:
                    node_result = future.result(timeout=node.timeout_seconds)
                    results[node_id] = node_result
                    shared.merge(node_result.state_updates)
                    state.outputs[node_id] = node_result.state_updates
                    state.node_status[node_id] = "completed"
                    logger.info("dag.node.complete", extra={"node_id": node_id, "agent_name": node.agent_name})
                except Exception as exc:
                    tb = traceback.format_exc()
                    state.node_status[node_id] = "failed"
                    state.errors[node_id] = tb
                    results[node_id] = DAGNodeResult(status="failed", logs=[str(exc), tb])
                    logger.error("dag.node.failed", extra={"node_id": node_id, "agent_name": node.agent_name})
                    if node.failure_policy == "fail_fast":
                        stop = True
                    if node.failure_policy in {"skip_dependents", "fail_fast"}:
                        skipped.update(_dependents_of(dag, node_id))

    for node_id in skipped:
        if state.node_status.get(node_id) == "pending":
            state.node_status[node_id] = "skipped"
            results[node_id] = DAGNodeResult(status="skipped", logs=["Skipped due to dependency failure."])

    completed = sum(1 for status in state.node_status.values() if status == "completed")
    failed = sum(1 for status in state.node_status.values() if status == "failed")
    skipped_count = sum(1 for status in state.node_status.values() if status == "skipped")
    return DAGExecutionResult(
        total_nodes=len(dag.nodes),
        completed_nodes=completed,
        failed_nodes=failed,
        skipped_nodes=skipped_count,
        execution_time=time.time() - start,
        node_results=results,
        parallel_groups=groups,
    )


def _dependents_of(dag: WorkflowDAG, node_id: str) -> set[str]:
    children = {}
    for edge in dag.edges:
        children.setdefault(edge.from_node, set()).add(edge.to_node)
    out = set()
    stack = list(children.get(node_id, set()))
    while stack:
        item = stack.pop()
        if item in out:
            continue
        out.add(item)
        stack.extend(children.get(item, set()))
    return out

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Literal, Optional


FailurePolicy = Literal["fail_fast", "continue_on_failure", "skip_dependents"]
NodeStatus = Literal["pending", "running", "completed", "failed", "skipped"]


@dataclass
class DAGNode:
    node_id: str
    agent_name: str
    loop_type: str = "digital"
    depends_on: List[str] = field(default_factory=list)
    required_inputs: List[str] = field(default_factory=list)
    produced_outputs: List[str] = field(default_factory=list)
    can_run_parallel: bool = True
    timeout_seconds: Optional[int] = None
    retry_count: int = 0
    failure_policy: FailurePolicy = "fail_fast"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DAGNode":
        return cls(
            node_id=str(data.get("node_id") or data.get("id") or ""),
            agent_name=str(data.get("agent_name") or data.get("label") or data.get("type") or ""),
            loop_type=str(data.get("loop_type") or "digital"),
            depends_on=[str(x) for x in (data.get("depends_on") or [])],
            required_inputs=[str(x) for x in (data.get("required_inputs") or [])],
            produced_outputs=[str(x) for x in (data.get("produced_outputs") or [])],
            can_run_parallel=bool(data.get("can_run_parallel", True)),
            timeout_seconds=data.get("timeout_seconds"),
            retry_count=int(data.get("retry_count") or 0),
            failure_policy=data.get("failure_policy") or "fail_fast",
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DAGEdge:
    from_node: str
    to_node: str

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DAGEdge":
        return cls(
            from_node=str(data.get("from_node") or data.get("source") or ""),
            to_node=str(data.get("to_node") or data.get("target") or ""),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class WorkflowDAG:
    nodes: List[DAGNode] = field(default_factory=list)
    edges: List[DAGEdge] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkflowDAG":
        return cls(
            nodes=[DAGNode.from_dict(x) for x in data.get("nodes", [])],
            edges=[DAGEdge.from_dict(x) for x in data.get("edges", [])],
            metadata=dict(data.get("metadata") or {}),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "workflow_mode": "dag",
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "metadata": dict(self.metadata),
        }


@dataclass
class DAGExecutionState:
    node_status: Dict[str, NodeStatus] = field(default_factory=dict)
    outputs: Dict[str, Any] = field(default_factory=dict)
    errors: Dict[str, str] = field(default_factory=dict)


@dataclass
class DAGNodeResult:
    status: str
    artifacts: Dict[str, Any] = field(default_factory=dict)
    logs: List[str] = field(default_factory=list)
    state_updates: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DAGExecutionResult:
    total_nodes: int
    completed_nodes: int
    failed_nodes: int
    skipped_nodes: int
    execution_time: float
    node_results: Dict[str, DAGNodeResult] = field(default_factory=dict)
    dry_run: bool = False
    parallel_groups: List[List[str]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_nodes": self.total_nodes,
            "completed_nodes": self.completed_nodes,
            "failed_nodes": self.failed_nodes,
            "skipped_nodes": self.skipped_nodes,
            "execution_time": self.execution_time,
            "node_results": {k: v.to_dict() for k, v in self.node_results.items()},
            "dry_run": self.dry_run,
            "parallel_groups": self.parallel_groups,
        }

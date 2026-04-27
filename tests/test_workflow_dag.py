import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workflow_dag.executor import execute_dag
from workflow_dag.models import DAGEdge, DAGNode, WorkflowDAG
from workflow_dag.planner import dag_from_agents, dry_run_plan, parallel_groups
from workflow_dag.validator import validate_dag


def _valid_dag() -> WorkflowDAG:
    return WorkflowDAG(
        metadata={"available_inputs": ["design_spec"]},
        nodes=[
            DAGNode(
                node_id="spec",
                agent_name="Digital Spec Agent",
                required_inputs=["design_spec"],
                produced_outputs=["normalized_spec"],
            ),
            DAGNode(
                node_id="rtl",
                agent_name="Digital RTL Agent",
                depends_on=["spec"],
                required_inputs=["normalized_spec"],
                produced_outputs=["rtl_files"],
            ),
            DAGNode(
                node_id="sim",
                agent_name="Digital Sim Agent",
                depends_on=["rtl"],
                required_inputs=["rtl_files"],
                produced_outputs=["simulation_report"],
            ),
        ],
        edges=[
            DAGEdge("spec", "rtl"),
            DAGEdge("rtl", "sim"),
        ],
    )


def test_valid_dag_validation():
    ok, errors = validate_dag(_valid_dag())

    assert ok
    assert errors == []


def test_cycle_detection():
    dag = _valid_dag()
    dag.edges.append(DAGEdge("sim", "spec"))
    dag.nodes[0].depends_on.append("sim")

    ok, errors = validate_dag(dag)

    assert not ok
    assert any("cycle" in error for error in errors)


def test_missing_agent_detection():
    dag = _valid_dag()
    dag.nodes[0].agent_name = "Missing Agent"

    ok, errors = validate_dag(dag)

    assert not ok
    assert any("missing registered agent" in error for error in errors)


def test_dependency_resolution_and_parallel_grouping():
    dag = dag_from_agents(
        [
            "Digital Spec Agent",
            "Digital RTL Agent",
            "Digital Sim Agent",
            "Embedded Firmware Integration Contract Agent",
            "System Software SDK Scaffold Agent",
            "System Integration Intent Agent",
        ],
        loop_type="system",
        infer_parallel=True,
    )

    groups = parallel_groups(dag)

    assert groups[0] == ["n1"]
    assert groups[2] == ["n3", "n4", "n5"]
    assert groups[3] == ["n6"]


def test_dry_run_mode_reports_groups():
    plan = dry_run_plan(_valid_dag())

    assert plan["dry_run"]
    assert plan["execution_order"] == ["spec", "rtl", "sim"]
    assert plan["parallel_groups"] == [["spec"], ["rtl"], ["sim"]]


def test_executor_integrates_with_agent_runtime():
    dag = _valid_dag()

    def spec_agent(state):
        state["normalized_spec"] = "spec"
        state["status"] = "spec ok"
        return state

    def rtl_agent(state):
        assert state["normalized_spec"] == "spec"
        state["rtl_files"] = ["top.sv"]
        state["status"] = "rtl ok"
        return state

    def sim_agent(state):
        assert state["rtl_files"] == ["top.sv"]
        state["simulation_report"] = "pass"
        state["status"] = "sim ok"
        return state

    result = execute_dag(
        dag,
        {
            "Digital Spec Agent": spec_agent,
            "Digital RTL Agent": rtl_agent,
            "Digital Sim Agent": sim_agent,
        },
        initial_state={"workflow_id": "wf-dag", "loop_type": "digital", "design_spec": "counter"},
    )

    assert result.completed_nodes == 3
    assert result.failed_nodes == 0
    assert result.node_results["sim"].state_updates["simulation_report"] == "pass"


def test_failure_policy_skip_dependents():
    dag = WorkflowDAG(
        metadata={"available_inputs": ["design_spec"]},
        nodes=[
            DAGNode("spec", "Digital Spec Agent", required_inputs=["design_spec"], produced_outputs=["normalized_spec"]),
            DAGNode(
                "rtl",
                "Digital RTL Agent",
                depends_on=["spec"],
                required_inputs=["normalized_spec"],
                produced_outputs=["rtl_files"],
                failure_policy="skip_dependents",
            ),
            DAGNode("sim", "Digital Sim Agent", depends_on=["rtl"], required_inputs=["rtl_files"]),
        ],
        edges=[DAGEdge("spec", "rtl"), DAGEdge("rtl", "sim")],
    )

    def spec_agent(state):
        state["normalized_spec"] = "spec"
        return state

    def failing_agent(state):
        raise RuntimeError("boom")

    result = execute_dag(
        dag,
        {"Digital Spec Agent": spec_agent, "Digital RTL Agent": failing_agent, "Digital Sim Agent": spec_agent},
        initial_state={"workflow_id": "wf-dag", "design_spec": "counter"},
    )

    assert result.completed_nodes == 1
    assert result.failed_nodes == 1
    assert result.skipped_nodes == 1
    assert result.node_results["sim"].status == "skipped"


def test_example_dag_validates_and_is_json_serializable():
    data = json.loads(Path("examples/dag/arch2rtl_dag_preview.json").read_text(encoding="utf-8"))
    dag = WorkflowDAG.from_dict(data)

    ok, errors = validate_dag(dag)

    assert ok, errors
    assert json.dumps(dag.to_dict())

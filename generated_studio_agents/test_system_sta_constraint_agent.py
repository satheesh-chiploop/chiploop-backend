from generated_studio_agents.system_sta_constraint_agent import run_agent


def test_generated_agent_stub_runs():
    state = {"workflow_id": "wf-generated", "loop_type": "system"}
    result = run_agent(state)
    assert result["generated_agent_stub"] == "System STA Constraint Agent"

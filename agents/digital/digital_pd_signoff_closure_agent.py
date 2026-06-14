from agents.system.system_pd_signoff_closure_agent import run_with_agent_name


AGENT_NAME = "Digital PD Signoff Closure Agent"


def run_agent(state: dict) -> dict:
    return run_with_agent_name(state, AGENT_NAME)

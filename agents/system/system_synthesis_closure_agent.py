from agents.digital.digital_synthesis_closure_agent import run_with_agent_name


AGENT_NAME = "System Synthesis Closure Agent"


def run_agent(state: dict) -> dict:
    return run_with_agent_name(state, AGENT_NAME)

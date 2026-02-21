import json
from utils.artifact_utils import save_text_artifact_and_record
from agents.analog._analog_llm import llm_text, safe_json_load

def run_agent(state: dict) -> dict:
    agent_name = "Analog Correlation Agent"
    workflow_id = state.get("workflow_id")
    preview_only = bool(state.get("preview_only"))

    spec = state.get("analog_spec") or {}
    sim_plan = state.get("analog_sim_plan") or {}

    # In your real implementation, you'd read actual sim results from artifacts.
    # For now, this agent produces a correlation *plan + report template*.
    if not workflow_id or not isinstance(spec, dict) or not spec:
        state["status"] = "❌ Missing workflow_id or analog_spec"
        return state

    prompt = f"""
You are correlating a behavioral model vs SPICE netlist.
Given spec and sim plan, produce:
- which metrics to compare
- suggested tolerances
- a markdown report template with pass/fail table

Return ONLY JSON:
{{
  "metrics_compare": [{{"metric":"gain_db","method":"ac","tolerance_pct":5}}],
  "delta_summary": {{"overall":"unknown","top_risks":["..."]}},
  "report_md": "# Correlation Report\\n..."
}}

SPEC:
{json.dumps(spec, indent=2)}

SIM PLAN:
{json.dumps(sim_plan, indent=2)}
"""

    out = llm_text(prompt)
    obj = safe_json_load(out)

    metrics = obj.get("metrics_compare") or []
    delta = obj.get("delta_summary") or {}
    report = (obj.get("report_md") or "").strip()

    state["analog_metrics_compare"] = metrics
    state["analog_delta_summary"] = delta

    if not preview_only:
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="analog",
            filename="metrics_compare.json",
            content=json.dumps(metrics, indent=2),
        )
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="analog",
            filename="delta_summary.json",
            content=json.dumps(delta, indent=2),
        )
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="analog",
            filename="correlation_report.md",
            content=report or "# Correlation Report\n\n(TBD)\n",
        )

    return state
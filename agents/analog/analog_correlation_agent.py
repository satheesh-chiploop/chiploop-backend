import json
from utils.artifact_utils import save_text_artifact_and_record
from agents.analog._analog_llm import llm_text, safe_json_load


def run_agent(state: dict) -> dict:
    agent_name = "Analog Correlation Agent"
    workflow_id = state.get("workflow_id")
    preview_only = bool(state.get("preview_only"))

    spec = state.get("analog_spec") or {}
    sim_plan = state.get("analog_sim_plan") or {}
    sim_metrics = state.get("analog_sim_metrics") or {}
    beh_metrics = state.get("analog_behavioral_metrics") or {}

    if not workflow_id or not isinstance(spec, dict) or not spec:
        state["status"] = "❌ Missing workflow_id or analog_spec"
        return state

    prompt = f"""
You are correlating a behavioral model vs SPICE netlist.

Given spec + sim plan + (optional) metrics, produce:
- correlation plan (what to compare + method + tolerance)
- a deltas list (even if values are NA when missing)
- a markdown report with a pass/fail/NA table

Return ONLY JSON:
{{
  "correlation_plan_md": "# Correlation Plan\\n...",
  "metrics_compare": [{{"metric":"psrr_db_1khz","method":"ac","tolerance_pct":5}}],
  "deltas": [{{"metric":"psrr_db_1khz","beh":null,"spice":null,"delta":null,"status":"NA"}}],
  "delta_summary": {{"overall":"unknown","top_risks":["..."]}},
  "report_md": "# Correlation Report\\n..."
}}

SPEC:
{json.dumps(spec, indent=2)}

SIM PLAN:
{json.dumps(sim_plan, indent=2)}

SIM METRICS (may be stub/empty):
{json.dumps(sim_metrics, indent=2)[:4000]}

BEHAVIORAL METRICS (may be empty):
{json.dumps(beh_metrics, indent=2)[:4000]}

Rules:
- No fake precision: if values are missing, mark status as NA.
- Keep tolerances engineering-level.
"""

    out = llm_text(prompt)
    obj = safe_json_load(out)
    if not isinstance(obj, dict):
        obj = {}

    plan_md = (obj.get("correlation_plan_md") or "").strip() or "# Correlation Plan\n\n(TBD)\n"
    metrics_compare = obj.get("metrics_compare") if isinstance(obj.get("metrics_compare"), list) else []
    deltas = obj.get("deltas") if isinstance(obj.get("deltas"), list) else []
    delta_summary = obj.get("delta_summary") if isinstance(obj.get("delta_summary"), dict) else {"overall": "unknown", "top_risks": []}
    report_md = (obj.get("report_md") or "").strip() or "# Correlation Report\n\n(TBD)\n"

    state["analog_metrics_compare"] = metrics_compare
    state["analog_deltas"] = deltas
    state["analog_delta_summary"] = delta_summary

    if not preview_only:
        # New scaffold outputs
        save_text_artifact_and_record(workflow_id, agent_name, "analog", "correlation/correlation_plan.md", plan_md)
        save_text_artifact_and_record(workflow_id, agent_name, "analog", "correlation/metrics_compare.json", json.dumps(metrics_compare, indent=2))
        save_text_artifact_and_record(workflow_id, agent_name, "analog", "correlation/deltas.json", json.dumps(deltas, indent=2))
        save_text_artifact_and_record(workflow_id, agent_name, "analog", "correlation/delta_summary.json", json.dumps(delta_summary, indent=2))
        save_text_artifact_and_record(workflow_id, agent_name, "analog", "correlation/correlation_report.md", report_md)

        # Legacy compatibility
        save_text_artifact_and_record(workflow_id, agent_name, "analog", "metrics_compare.json", json.dumps(metrics_compare, indent=2))
        save_text_artifact_and_record(workflow_id, agent_name, "analog", "delta_summary.json", json.dumps(delta_summary, indent=2))
        save_text_artifact_and_record(workflow_id, agent_name, "analog", "correlation_report.md", report_md)

    return state
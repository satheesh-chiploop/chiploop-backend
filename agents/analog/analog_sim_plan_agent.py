import json
from utils.artifact_utils import save_text_artifact_and_record
from agents.analog._analog_llm import llm_text, safe_json_load

def run_agent(state: dict) -> dict:
    agent_name = "Analog Simulation Plan Agent"
    workflow_id = state.get("workflow_id")
    preview_only = bool(state.get("preview_only"))

    spec = state.get("analog_spec") or {}
    netlist = state.get("analog_netlist") or ""
    if not workflow_id or not isinstance(spec, dict) or not spec:
        state["status"] = "❌ Missing workflow_id or analog_spec"
        return state

    prompt = f"""
You are an analog verification engineer.

Given:
SPEC JSON:
{json.dumps(spec, indent=2)}

NETLIST (may be scaffold):
{netlist[:4000]}

Return ONLY valid JSON (no markdown) with schema:
{{
  "sweeps": {{
    "dc": [{{"name":"dc_vin_sweep","source":"VIN","start":0.0,"stop":0.0,"step":0.0}}],
    "ac": [{{"name":"ac_bode","start_hz":10,"stop_hz":1e9,"points_per_dec":20}}],
    "tran": [{{"name":"tran_step","tstop_s":0.001,"tstep_s":1e-6,"stimulus":"enable+load_step"}}]
  }},
  "corners": {{
    "vdd": {spec.get("corners",{}).get("vdd",[])},
    "temp_c": {spec.get("corners",{}).get("temp_c",[])},
    "process": {spec.get("corners",{}).get("process",[])}
  }},
  "metrics": [
    {{"name":"vout_steady","method":"steady_state","signal":"VOUT","units":"V"}},
    {{"name":"settling_time","method":"settling","signal":"VOUT","units":"s"}}
  ],
  "tolerances": {{
    "default_pct": 5.0,
    "default_abs": null
  }},
  "notes": ["..."]
}}
"""

    out = llm_text(prompt)
    plan = safe_json_load(out)
    state["analog_sim_plan"] = plan

    if not preview_only:
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="analog",
            filename="sim_plan.json",
            content=json.dumps(plan, indent=2),
        )

        # Minimal example run deck template
        deck = ".include netlist.sp\n* TODO: add sources, analysis commands based on sim_plan.json\n.end\n"
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="analog",
            filename="run_deck.sp",
            content=deck,
        )

    return state
from utils.artifact_utils import save_text_artifact_and_record
from agents.runtime import RUNTIME_ACTIVE_STATE_KEY, AgentContext, execute_agent
import os
import json
import glob

AGENT_NAME = "Analog Executive Summary Agent"

def _fmt(v):
    return "NA" if v is None else str(v)


def _get_requirements(state: dict) -> dict:
    req = state.get("analog_requirements")
    if isinstance(req, dict) and isinstance(req.get("requirements"), list):
        return req
    # fallback: try from spec.targets
    spec = state.get("analog_spec") or {}
    requirements = []
    targets = (spec.get("targets") or {}) if isinstance(spec, dict) else {}
    for domain in ("dc", "ac", "transient"):
        for it in (targets.get(domain) or []):
            if isinstance(it, dict):
                requirements.append(
                    {
                        "domain": domain,
                        "metric": it.get("metric"),
                        "min": it.get("min"),
                        "max": it.get("max"),
                        "units": it.get("units"),
                        "source": "spec",
                        "confidence": "high",
                    }
                )
    return {"requirements": requirements, "notes": ["requirements derived from spec.targets (fallback)"]}


def _run(context: AgentContext) -> dict:
    state = context.state
    agent_name = context.agent_name
    workflow_id = state.get("workflow_id")
    preview_only = bool(state.get("preview_only"))

    if not workflow_id:
        state["status"] = "❌ Missing workflow_id"
        return state

    spec = state.get("analog_spec") or {}
    req = _get_requirements(state)



    sim_metrics = state.get("analog_sim_metrics") or {}
    beh_metrics = state.get("analog_behavioral_metrics") or {}

    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")

    if not sim_metrics:
        metrics_path = os.path.join(workflow_dir, "analog", "sim", "results", "metrics.json")
        if os.path.exists(metrics_path):
            try:
                with open(metrics_path) as f:
                    sim_metrics = json.load(f)
            except Exception:
                sim_metrics = {}

    if not beh_metrics:
        beh_metrics_path = os.path.join(workflow_dir, "analog", "behavioral", "metrics.json")
        if os.path.exists(beh_metrics_path):
            try:
                with open(beh_metrics_path) as f:
                    beh_metrics = json.load(f)
            except Exception:
                beh_metrics = {}



    delta = state.get("analog_delta_summary") or {}
    deltas = state.get("analog_deltas") or []

    if not delta:
        delta_path = os.path.join(workflow_dir, "analog", "correlation", "delta_summary.json")
        if os.path.exists(delta_path):
            try:
                with open(delta_path) as f:
                   delta = json.load(f)
            except Exception:
                delta = {}

    if not deltas:
        deltas_path = os.path.join(workflow_dir, "analog", "correlation", "deltas.json")
        if os.path.exists(deltas_path):
            try:
                with open(deltas_path) as f:
                    deltas = json.load(f)
            except Exception:
                deltas = []

    open_q = (spec.get("open_questions") or []) if isinstance(spec, dict) else []
    assumptions = (spec.get("assumptions") or []) if isinstance(spec, dict) else []

    sim_vals = (sim_metrics.get("metrics") or {}) if isinstance(sim_metrics, dict) else {}
    beh_vals = (beh_metrics.get("metrics") or {}) if isinstance(beh_metrics, dict) else {}
    sim_conf = sim_metrics.get("confidence") if isinstance(sim_metrics, dict) else None
    beh_conf = beh_metrics.get("confidence") if isinstance(beh_metrics, dict) else None
    lines = []
    lines.append("# Analog Executive Summary")
    lines.append("")
    block_name = spec.get("block_name") or ((spec.get("block") or {}).get("name")) or "(unknown)"

    module_name = spec.get("module_name") or (
        f"{block_name}_model" if block_name and block_name != "(unknown)" else "(unknown)"
    )
    

    lines.append(f"- Block: {block_name}")
    lines.append(f"- Module: {module_name}")

    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")

    model_present = os.path.exists(os.path.join(workflow_dir, "analog", "model.sv"))
    tb_present = any([
        os.path.exists(os.path.join(workflow_dir, "analog", "behavioral", f"tb_{block_name}_behavioral.sv")),
        os.path.exists(os.path.join(workflow_dir, "analog", "tb.sv")),
    ])
    netlist_present = any([
        os.path.exists(os.path.join(workflow_dir, "analog", "netlist", f"{block_name}_top.sp")),
        os.path.exists(os.path.join(workflow_dir, "analog", "netlist.sp")),
    ])

    sim_plan_present = (
        os.path.exists(os.path.join(workflow_dir, "analog", "sim", "sim_plan.json"))
        or os.path.exists(os.path.join(workflow_dir, "analog", "sim_plan.json"))
    )

    corr_present = (
       os.path.exists(os.path.join(workflow_dir, "analog", "correlation", "delta_summary.json"))
       or os.path.exists(os.path.join(workflow_dir, "analog", "delta_summary.json"))
    )


    abstract_present = bool(
       glob.glob(os.path.join(workflow_dir, "analog", "abstract", "*.lef")) or
       glob.glob(os.path.join(workflow_dir, "analog", "abstract", "*.lib"))
    )


    lines.append("")
    lines.append("## Artifact Presence")
    lines.append(f"- Behavioral model: {'present' if model_present else 'missing'}")
    lines.append(f"- Behavioral testbench: {'present' if tb_present else 'missing'}")
    lines.append(f"- Netlist scaffold: {'present' if netlist_present else 'missing'}")
    lines.append(f"- Simulation plan: {'present' if sim_plan_present else 'missing'}")
    lines.append(f"- Correlation: {'present' if corr_present else 'missing'}")
    lines.append(f"- Abstract views: {'present' if abstract_present else 'missing'}")
    lines.append("")


    lines.append("## Metrics Confidence")
    lines.append(f"- Simulation metrics confidence: {_fmt(sim_conf)}")
    lines.append(f"- Behavioral metrics confidence: {_fmt(beh_conf)}")
    lines.append("")
    

    # Compliance table
    lines.append("## Spec Compliance Table")
    lines.append("")
    lines.append("| Domain | Metric | Target (min..max) | Sim | Behavioral | Status | Source |")
    lines.append("|---|---|---:|---:|---:|---|---|")

    for r in (req.get("requirements") or []):
        metric = r.get("metric")
        tmin = r.get("min")
        tmax = r.get("max")
        units = r.get("units") or ""
        target = f"{_fmt(tmin)}..{_fmt(tmax)} {units}".strip()
        sim_v = sim_vals.get(metric) if isinstance(sim_vals, dict) else None
        beh_v = beh_vals.get(metric) if isinstance(beh_vals, dict) else None

        status = "NA"
        if sim_v is not None or beh_v is not None:
            ok = True
            for v in (sim_v, beh_v):
                if v is None:
                    continue
                try:
                    fv = float(v)
                except Exception:
                    ok = False
                    break
                if tmin is not None and fv < float(tmin):
                    ok = False
                if tmax is not None and fv > float(tmax):
                    ok = False
            status = "PASS" if ok else "FAIL"

        lines.append(
            f"| {r.get('domain')} | {metric} | {target} | {_fmt(sim_v)} | {_fmt(beh_v)} | {status} | {r.get('source','spec')} |"
        )

    lines.append("")
    lines.append("## Correlation Summary")
    lines.append(f"- Correlation summary: {delta.get('overall') if isinstance(delta, dict) else 'unknown'}")
    if isinstance(delta, dict) and delta.get("top_risks"):
        lines.append("### Top Risks")
        for r in delta.get("top_risks", []):
            lines.append(f"- {r}")

    if deltas:
        lines.append("")
        lines.append("## Correlation Deltas (Top)")
        for d in deltas[:10]:
            if isinstance(d, dict):
                lines.append(
                    f"- {d.get('metric')}: beh={_fmt(d.get('beh'))}, spice={_fmt(d.get('spice'))}, delta={_fmt(d.get('delta'))}, status={d.get('status','NA')}"
                )

    lines.append("")
    lines.append("## How to Run")
    lines.append("```bash")
    lines.append("SIM_FLAVOR=ngspice SPICE_BIN=ngspice ./analog/sim/run_all.sh")
    lines.append("```")

    lines.append("")
    lines.append("## Artifact Paths")
    lines.append("- Spec: analog/spec/")
    lines.append("- Netlist: analog/netlist/")
    lines.append("- Simulation: analog/sim/")
    lines.append("- Behavioral: analog/behavioral/")
    lines.append("- Correlation: analog/correlation/")
    lines.append("- Iteration: analog/iteration/")
    lines.append("- Abstract: analog/abstract/")

    lines.append("")
    lines.append("## Open Questions")
    lines.extend([f"- {q}" for q in open_q] or ["- None"])

    lines.append("")
    lines.append("## Assumptions")
    lines.extend([f"- {a}" for a in assumptions] or ["- None"])

    content = "\n".join(lines).strip() + "\n"
    state["analog_exec_summary_md"] = content

    if not preview_only:
        save_text_artifact_and_record(workflow_id, agent_name, "analog", "executive_summary.md", content)

    return state


def run_agent(state: dict) -> dict:
    context = AgentContext.from_state(state, AGENT_NAME)
    if state.get(RUNTIME_ACTIVE_STATE_KEY):
        return _run(context)
    result = execute_agent(context, _run)
    state.update(result.to_state_update())
    return state

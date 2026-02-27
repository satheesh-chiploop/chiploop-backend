from utils.artifact_utils import save_text_artifact_and_record


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


def run_agent(state: dict) -> dict:
    agent_name = "Analog Executive Summary Agent"
    workflow_id = state.get("workflow_id")
    preview_only = bool(state.get("preview_only"))

    if not workflow_id:
        state["status"] = "❌ Missing workflow_id"
        return state

    spec = state.get("analog_spec") or {}
    req = _get_requirements(state)

    sim_metrics = state.get("analog_sim_metrics") or {}
    beh_metrics = state.get("analog_behavioral_metrics") or {}

    delta = state.get("analog_delta_summary") or {}
    deltas = state.get("analog_deltas") or []

    open_q = (spec.get("open_questions") or []) if isinstance(spec, dict) else []
    assumptions = (spec.get("assumptions") or []) if isinstance(spec, dict) else []

    sim_vals = (sim_metrics.get("metrics") or {}) if isinstance(sim_metrics, dict) else {}
    beh_vals = (beh_metrics.get("metrics") or {}) if isinstance(beh_metrics, dict) else {}

    lines = []
    lines.append("# Analog Executive Summary")
    lines.append("")
    lines.append(f"- Block: {((spec.get('block') or {}).get('name')) if isinstance(spec, dict) else '(unknown)'}")
    lines.append(f"- Type:  {((spec.get('block') or {}).get('type')) if isinstance(spec, dict) else '(unknown)'}")
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
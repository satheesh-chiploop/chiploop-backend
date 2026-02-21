import json
from utils.artifact_utils import save_text_artifact_and_record

def run_agent(state: dict) -> dict:
    agent_name = "Analog Executive Summary Agent"
    workflow_id = state.get("workflow_id")
    preview_only = bool(state.get("preview_only"))

    if not workflow_id:
        state["status"] = "❌ Missing workflow_id"
        return state

    spec = state.get("analog_spec") or {}
    delta = state.get("analog_delta_summary") or {}
    open_q = (spec.get("open_questions") or []) if isinstance(spec, dict) else []
    assumptions = (spec.get("assumptions") or []) if isinstance(spec, dict) else []

    lines = []
    lines.append("# Analog Executive Summary")
    lines.append("")
    lines.append(f"- Block: {((spec.get('block') or {}).get('name')) if isinstance(spec, dict) else '(unknown)'}")
    lines.append(f"- Type:  {((spec.get('block') or {}).get('type')) if isinstance(spec, dict) else '(unknown)'}")
    lines.append("")
    lines.append("## Current Status")
    lines.append(f"- Correlation summary: {delta.get('overall') if isinstance(delta, dict) else 'unknown'}")
    if isinstance(delta, dict) and delta.get("top_risks"):
        lines.append("### Top Risks")
        for r in delta.get("top_risks", []):
            lines.append(f"- {r}")

    lines.append("")
    lines.append("## Open Questions")
    lines.extend([f"- {q}" for q in open_q] or ["- None"])

    lines.append("")
    lines.append("## Assumptions")
    lines.extend([f"- {a}" for a in assumptions] or ["- None"])

    content = "\n".join(lines).strip() + "\n"
    state["analog_exec_summary_md"] = content

    if not preview_only:
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="analog",
            filename="executive_summary.md",
            content=content,
        )

    return state
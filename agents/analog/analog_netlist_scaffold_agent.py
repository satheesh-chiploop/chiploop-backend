import json
from utils.artifact_utils import save_text_artifact_and_record
from agents.analog._analog_llm import llm_text


def _ensure_models_include(netlist: str) -> str:
    inc_line = '.include "models/models.placeholder.inc"'
    if inc_line in netlist:
        return netlist
    # Prepend include near the top
    lines = netlist.splitlines()
    out = []
    inserted = False
    for i, line in enumerate(lines):
        out.append(line)
        if not inserted and (line.strip().startswith("*") or line.strip() == "") and i < 10:
            # keep scanning first few comment/blank lines
            continue
        if not inserted:
            out.insert(0, inc_line)
            inserted = True
            break
    if not inserted:
        return inc_line + "\n" + netlist
    return "\n".join(out)


def run_agent(state: dict) -> dict:
    agent_name = "Analog Netlist Scaffold Agent"
    workflow_id = state.get("workflow_id")
    preview_only = bool(state.get("preview_only"))

    spec = state.get("analog_spec") or {}
    if not workflow_id or not isinstance(spec, dict) or not spec:
        state["status"] = "❌ Missing workflow_id or analog_spec"
        return state

    prompt = f"""
You are an analog circuit designer.

Given this JSON spec:
{json.dumps(spec, indent=2)}

Generate a SPICE netlist scaffold as plain text (not markdown).

Production rules:
- NO PDK hardcoding (no sky130/tsmc/.lib tech files). Use placeholders only.
- Must include: .include "models/models.placeholder.inc"
- Use placeholders <NMOS_MODEL> and <PMOS_MODEL> in device lines (or comment where to replace).
- Include .SUBCKT with pin order matching supplies + pins listed in spec.
- Include placeholder blocks: pass device, error amp, reference placeholder, compensation, feedback divider.
- Include .ENDS

Return ONLY netlist text.
"""

    netlist = llm_text(prompt).strip()
    if not netlist:
        netlist = "* (empty netlist scaffold)\n"

    # enforce include line
    netlist = _ensure_models_include(netlist)

    state["analog_netlist"] = netlist

    models_inc = (
        "* PDK-agnostic model placeholders\n"
        "* Replace these with real model includes for your target PDK\n"
        "* Example:\n"
        "*   .include /path/to/your_pdk_models.lib\n"
        "*   .lib /path/to/your_pdk_models.lib TT\n"
        "*\n"
        "* Required placeholders:\n"
        ".param NMOS_MODEL=<NMOS_MODEL>\n"
        ".param PMOS_MODEL=<PMOS_MODEL>\n"
    )

    readme = (
        "# Netlist Scaffold (PDK-agnostic)\n\n"
        "This folder contains a PDK-agnostic LDO scaffold netlist.\n\n"
        "## How to make it runnable\n"
        "1) Edit `models/models.placeholder.inc` to point to your PDK model library.\n"
        "2) Replace `<NMOS_MODEL>` / `<PMOS_MODEL>` placeholders with model names in your simulator/PDK.\n"
        "3) Run deck templates via `analog/sim/run_all.sh` (choose SIM_FLAVOR + SPICE_BIN).\n\n"
        "## Production rules\n"
        "- No hardcoded PDK or foundry-specific includes in generated artifacts.\n"
        "- Keep netlist generic; technology-specific content must be injected by the user at run time.\n"
    )

    if not preview_only:
        # New scaffold layout
        save_text_artifact_and_record(workflow_id, agent_name, "analog", "netlist/ldo_top.sp", netlist)
        save_text_artifact_and_record(workflow_id, agent_name, "analog", "netlist/models/models.placeholder.inc", models_inc)
        save_text_artifact_and_record(workflow_id, agent_name, "analog", "netlist/README.md", readme)

        # Legacy compatibility
        save_text_artifact_and_record(workflow_id, agent_name, "analog", "netlist.sp", netlist)

        block_name = (spec.get("block") or {}).get("name", "(unknown)")
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="analog",
            filename="netlist_summary.md",
            content=f"# Netlist Scaffold Summary\n\nGenerated a PDK-agnostic SPICE scaffold for: {block_name}\n",
        )

    return state
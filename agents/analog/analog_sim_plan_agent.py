import json
from utils.artifact_utils import save_text_artifact_and_record
from agents.analog._analog_llm import llm_text, safe_json_load


def _bash_header() -> str:
    return "#!/usr/bin/env bash\nset -euo pipefail\n"


def _flavor_run_all_script(deck_names) -> str:
    deck_loop = "\n".join(
        [f'for d in {" ".join(deck_names)}; do'] +
        [
            '  if [[ -f "${DECK_DIR}/${d}" ]]; then',
            '    echo "  -> ${d}"',
            '    "${SPICE_BIN}" -b "${DECK_DIR}/${d}" > "${OUT_DIR}/${d}.log" 2>&1 || true',
            '  fi',
            'done',
        ]
    )
    return _bash_header() + (
        'source "$(dirname "$0")/../env.sh"\n'
        'DECK_DIR="$(cd "$(dirname "$0")" && pwd)/decks"\n'
        'OUT_DIR="${ANALOG_ROOT}/sim/results/raw/${SIM_FLAVOR}"\n'
        'mkdir -p "${OUT_DIR}"\n'
        '\n'
        'echo "[analog] Running ${SIM_FLAVOR} deck templates (best-effort)..." \n'
        f'{deck_loop}\n'
        '\n'
        'python3 "${ANALOG_ROOT}/sim/parse/extract_metrics.py" '
        '--raw_dir "${ANALOG_ROOT}/sim/results/raw" '
        '--out "${ANALOG_ROOT}/sim/results/metrics.json"\n'
    )


def _deck_header(netlist_name: str) -> str:
    return (
        "* PDK-agnostic deck template\n"
        "* Netlist include (edit if needed):\n"
        f'.include "../../netlist/{netlist_name}"\n'
        "\n"
    )


def _extract_metrics_py() -> str:
    return """#!/usr/bin/env python3
import argparse
import json
import os

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw_dir", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    metrics = {
        "source": "sim",
        "confidence": "low",
        "notes": ["Metric extraction stub. Extend once simulator parsing is finalized."],
        "metrics": {},
        "found_logs": []
    }

    for root, _, files in os.walk(args.raw_dir):
        for f in files:
            if f.endswith(".log"):
                metrics["found_logs"].append(os.path.join(root, f))

    with open(args.out, "w", encoding="utf-8") as fp:
        json.dump(metrics, fp, indent=2)

if __name__ == "__main__":
    main()
"""


def run_agent(state: dict) -> dict:
    agent_name = "Analog Simulation Plan Agent"
    workflow_id = state.get("workflow_id", "default")
    preview_only = bool(state.get("preview_only"))

    spec = state.get("analog_spec") or {}
    if not isinstance(spec, dict) or not spec:
        state["status"] = "❌ Missing analog_spec"
        return state

    block_name = spec.get("block_name", "analog_block")
    netlist_name = f"{block_name}_top.sp"
    ports = spec.get("ports") or []
    port_names = {p.get("name") for p in ports if isinstance(p, dict)}

    prompt = f"""
You are an analog verification engineer.

Given SPEC JSON:
{json.dumps(spec, indent=2)}

Return ONLY valid JSON (no markdown) with schema:
{{
  "sweeps": {{
    "dc": [],
    "ac": [],
    "tran": []
  }},
  "corners": {{
    "vdd": [3.0, 3.3, 3.6],
    "temp_c": [-40, 25, 125],
    "process": ["typical", "fast", "slow"]
  }},
  "metrics": [],
  "tolerances": {{
    "default_pct": 5.0,
    "default_abs": null
  }},
  "notes": ["..."]
}}

Rules:
- Make the plan spec-driven, not LDO-driven.
- If ADC-style ports exist, include conversion-oriented transient scenarios.
- If regulator-style ports exist, include enable/power-good or output-settle scenarios.
- Avoid hardcoding loopgain/PSRR unless clearly justified by the spec.
- Keep it PDK-agnostic and simulator-agnostic.
- Do NOT claim silicon-grade precision.
"""

    out = llm_text(prompt)
    plan = safe_json_load(out)

    if not isinstance(plan, dict) or not plan:
        # Safe fallback plan
        plan = {
            "sweeps": {
                "dc": [],
                "ac": [],
                "tran": []
            },
            "corners": {
                "vdd": [3.0, 3.3, 3.6],
                "temp_c": [-40, 25, 125],
                "process": ["typical", "fast", "slow"]
            },
            "metrics": [],
            "tolerances": {"default_pct": 5.0, "default_abs": None},
            "notes": ["Fallback spec-driven plan used because LLM output was unavailable or invalid."]
        }

        if {"adc_clk", "adc_start", "adc_done", "adc_data"} & port_names:
            plan["sweeps"]["tran"].append({
                "name": "tran_adc_conversion",
                "tstop_s": 10e-6,
                "tstep_s": 10e-9,
                "stimulus": "adc_start_pulse"
            })
            plan["metrics"].extend([
                {"name": "adc_done_latency_s", "method": "latency", "signal": "adc_done", "units": "s"},
                {"name": "adc_data_valid", "method": "event_data_valid", "signal": "adc_data", "units": "bool"},
            ])

        if {"ldo_enable", "power_good"} & port_names:
            plan["sweeps"]["tran"].append({
                "name": "tran_ldo_enable",
                "tstop_s": 50e-6,
                "tstep_s": 100e-9,
                "stimulus": "ldo_enable_toggle"
            })
            plan["metrics"].extend([
                {"name": "power_good_delay_s", "method": "latency", "signal": "power_good", "units": "s"},
            ])

    state["analog_sim_plan"] = plan

    env_sh = _bash_header() + (
        'export SIM_FLAVOR="${SIM_FLAVOR:-ngspice}"\n'
        'export SPICE_BIN="${SPICE_BIN:-ngspice}"\n'
        'export ANALOG_ROOT="${ANALOG_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"\n'
    )

    run_all_sh = _bash_header() + (
        'source "$(dirname "$0")/env.sh"\n'
        'FLAVOR="${SIM_FLAVOR}"\n'
        'case "$FLAVOR" in\n'
        '  ngspice|spectre|hspice) ;;\n'
        '  *) echo "Unsupported SIM_FLAVOR=$FLAVOR (use ngspice|spectre|hspice)"; exit 2 ;;\n'
        'esac\n'
        'exec "${ANALOG_ROOT}/sim/${FLAVOR}/run_all.sh"\n'
    )

    deck_names = ["dc_op.sp"]
    dc_decks = []
    ac_decks = []
    tran_decks = []

    for item in plan.get("sweeps", {}).get("dc", []):
        nm = item.get("name", "dc_generic")
        fname = f"{nm}.sp"
        deck_names.append(fname)
        dc_decks.append((fname, _deck_header(netlist_name) + f"* DC deck: {nm}\n.op\n.end\n"))

    for item in plan.get("sweeps", {}).get("ac", []):
        nm = item.get("name", "ac_generic")
        fname = f"{nm}.sp"
        deck_names.append(fname)
        ac_decks.append((fname, _deck_header(netlist_name) + f"* AC deck: {nm}\n.ac dec 20 10 1e6\n.end\n"))

    for item in plan.get("sweeps", {}).get("tran", []):
        nm = item.get("name", "tran_generic")
        fname = f"{nm}.sp"
        deck_names.append(fname)
        tran_decks.append((fname, _deck_header(netlist_name) + f"* TRAN deck: {nm}\n.tran 1n 10u\n.end\n"))

    if not preview_only:
        save_text_artifact_and_record(workflow_id, agent_name, "analog", "sim_plan.json", json.dumps(plan, indent=2))
        save_text_artifact_and_record(workflow_id, agent_name, "analog", "run_deck.sp", _deck_header(netlist_name) + ".op\n.end\n")

        save_text_artifact_and_record(workflow_id, agent_name, "analog", "sim/sim_plan.json", json.dumps(plan, indent=2))
        save_text_artifact_and_record(workflow_id, agent_name, "analog", "sim/env.sh", env_sh)
        save_text_artifact_and_record(workflow_id, agent_name, "analog", "sim/run_all.sh", run_all_sh)

        for flavor in ("ngspice", "spectre", "hspice"):
            save_text_artifact_and_record(workflow_id, agent_name, "analog", f"sim/{flavor}/run_all.sh", _flavor_run_all_script(deck_names))
            save_text_artifact_and_record(workflow_id, agent_name, "analog", f"sim/{flavor}/decks/dc_op.sp", _deck_header(netlist_name) + ".op\n.end\n")
            for fname, text in dc_decks + ac_decks + tran_decks:
                save_text_artifact_and_record(workflow_id, agent_name, "analog", f"sim/{flavor}/decks/{fname}", text)

        save_text_artifact_and_record(workflow_id, agent_name, "analog", "sim/parse/extract_metrics.py", _extract_metrics_py())
        metrics_stub = {
            "source": "sim",
            "confidence": "low",
            "notes": ["Initial stub metrics; generated before any real simulator parsing"],
            "metrics": {},
        }
        save_text_artifact_and_record(
            workflow_id,
            agent_name,
            "analog",
            "sim/results/metrics.json",
            json.dumps(metrics_stub, indent=2)
        )
        state["analog_sim_metrics"] = metrics_stub
    state["analog_sim_plan"] = plan
    state["analog_sim_plan_path"] = "analog/sim/sim_plan.json"
    state["analog_sim_metrics_path"] = "analog/sim/results/metrics.json"
    state["analog_run_deck_path"] = "analog/run_deck.sp"


    return state
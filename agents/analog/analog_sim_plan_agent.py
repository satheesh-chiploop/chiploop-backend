import json
import textwrap
from utils.artifact_utils import save_text_artifact_and_record
from agents.analog._analog_llm import llm_text, safe_json_load


def _bash_header() -> str:
    return "#!/usr/bin/env bash\nset -euo pipefail\n"


def _flavor_run_all_script() -> str:
    """
    Generic runner used for ngspice/spectre/hspice folders.
    Uses SIM_FLAVOR + SPICE_BIN and runs the deck templates in a best-effort way.
    """
    return _bash_header() + (
        'source "$(dirname "$0")/../env.sh"\n'
        'DECK_DIR="$(cd "$(dirname "$0")" && pwd)/decks"\n'
        'OUT_DIR="${ANALOG_ROOT}/sim/results/raw/${SIM_FLAVOR}"\n'
        'mkdir -p "${OUT_DIR}"\n'
        '\n'
        'echo "[analog] Running ${SIM_FLAVOR} deck templates (placeholders; best-effort)..." \n'
        'for d in dc_op.sp dc_sweep_vin.sp ac_loopgain.sp ac_psrr.sp tran_loadstep.sp; do\n'
        '  if [[ -f "${DECK_DIR}/${d}" ]]; then\n'
        '    echo "  -> ${d}"\n'
        '    "${SPICE_BIN}" -b "${DECK_DIR}/${d}" > "${OUT_DIR}/${d}.log" 2>&1 || true\n'
        '  fi\n'
        'done\n'
        '\n'
        'python3 "${ANALOG_ROOT}/sim/parse/extract_metrics.py" '
        '--raw_dir "${ANALOG_ROOT}/sim/results/raw" '
        '--out "${ANALOG_ROOT}/sim/results/metrics.json"\n'
    )


def _deck_header() -> str:
    # NOTE: SPICE doesn't expand ${} by default; this is a template.
    # The runner does not substitute variables; users can adjust includes as needed.
    return (
        "* PDK-agnostic deck template\n"
        '* Netlist include (edit if needed):\n'
        '.include "../../netlist/ldo_top.sp"\n'
        '* Models include is expected inside ldo_top.sp: .include "models/models.placeholder.inc"\n'
        "\n"
    )


def _extract_metrics_py() -> str:
    return textwrap.dedent(
        r"""\
        #!/usr/bin/env python3
        import argparse
        import json
        import os

        def main():
            ap = argparse.ArgumentParser()
            ap.add_argument("--raw_dir", required=True)
            ap.add_argument("--out", required=True)
            args = ap.parse_args()

            # Stub extractor: compile-ready placeholder.
            # Extend later once you standardize simulator outputs (log, raw, psf, etc).
            metrics = {
                "source": "sim",
                "confidence": "low",
                "notes": [
                    "Metric extraction stub. Extend with real parsing once simulator log/wave formats are finalized."
                ],
                "metrics": {},
                "found_logs": []
            }

            for root, _, files in os.walk(args.raw_dir):
                for f in files:
                    if f.endswith(".log"):
                        metrics["found_logs"].append(os.path.join(root, f))

            with open(args.out, "w", encoding="utf-8") as fp:
                json.dump(metrics, fp, indent=2)
            return 0

        if __name__ == "__main__":
            raise SystemExit(main())
        """
    )


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
    "dc": [
      {{"name":"dc_vin_sweep","source":"VIN","start":null,"stop":null,"step":null}},
      {{"name":"dc_load_sweep","source":"ILOAD","start":null,"stop":null,"step":null}}
    ],
    "ac": [
      {{"name":"ac_loopgain","start_hz":10,"stop_hz":1e6,"points_per_dec":20}},
      {{"name":"ac_psrr","start_hz":10,"stop_hz":1e6,"points_per_dec":20}}
    ],
    "tran": [
      {{"name":"tran_loadstep","tstop_s":0.002,"tstep_s":1e-6,"stimulus":"enable+load_step"}}
    ]
  }},
  "corners": {{
    "vdd": {spec.get("corners",{}).get("vdd",[])},
    "temp_c": {spec.get("corners",{}).get("temp_c",[])},
    "process": {spec.get("corners",{}).get("process",[])}
  }},
  "metrics": [
    {{"name":"vout_v","method":"steady_state","signal":"VOUT","units":"V"}},
    {{"name":"dropout_v","method":"dropout","signal":"VOUT","units":"V"}},
    {{"name":"psrr_db_1khz","method":"psrr","signal":"VOUT","units":"dB"}},
    {{"name":"phase_margin_deg","method":"loopgain","signal":"VOUT","units":"deg"}},
    {{"name":"settling_time_s","method":"settling","signal":"VOUT","units":"s"}}
  ],
  "tolerances": {{
    "default_pct": 5.0,
    "default_abs": null
  }},
  "notes": ["..."]
}}

Rules:
- Keep it PDK-agnostic and simulator-agnostic.
- Do NOT claim silicon-grade precision. Keep templates and placeholders.
"""

    out = llm_text(prompt)
    plan = safe_json_load(out)
    if not isinstance(plan, dict) or not plan:
        plan = {"sweeps": {}, "corners": {}, "metrics": [], "tolerances": {"default_pct": 5.0, "default_abs": None}, "notes": ["plan parse failed; using empty plan"]}

    state["analog_sim_plan"] = plan

    # Scripts
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

    # Deck templates (placeholders)
    dc_op = _deck_header() + ".op\n.end\n"
    dc_sweep_vin = _deck_header() + (
        "* VIN sweep placeholder (edit values per spec)\n"
        "* VVIN VIN 0 1.5\n"
        ".dc VVIN 1.3 2.0 0.05\n"
        ".end\n"
    )
    ac_loopgain = _deck_header() + (
        "* Loopgain placeholder: implement loop-break method per simulator\n"
        ".ac dec 20 10 1e6\n"
        ".end\n"
    )
    ac_psrr = _deck_header() + (
        "* PSRR placeholder: inject small-signal ripple on VIN\n"
        ".ac dec 20 10 1e6\n"
        ".end\n"
    )
    tran_loadstep = _deck_header() + (
        "* Load step placeholder: implement ILOAD step on VOUT\n"
        ".tran 1u 2m\n"
        ".end\n"
    )

    # Legacy stub run deck
    legacy_deck = ".include netlist.sp\n* TODO: use analog/sim/<flavor>/decks/*.sp\n.end\n"

    if not preview_only:
        # Legacy outputs
        save_text_artifact_and_record(workflow_id, agent_name, "analog", "sim_plan.json", json.dumps(plan, indent=2))
        save_text_artifact_and_record(workflow_id, agent_name, "analog", "run_deck.sp", legacy_deck)

        # New scaffold outputs
        save_text_artifact_and_record(workflow_id, agent_name, "analog", "sim/sim_plan.json", json.dumps(plan, indent=2))
        save_text_artifact_and_record(workflow_id, agent_name, "analog", "sim/env.sh", env_sh)
        save_text_artifact_and_record(workflow_id, agent_name, "analog", "sim/run_all.sh", run_all_sh)

        for flavor in ("ngspice", "spectre", "hspice"):
            save_text_artifact_and_record(workflow_id, agent_name, "analog", f"sim/{flavor}/run_all.sh", _flavor_run_all_script())
            save_text_artifact_and_record(workflow_id, agent_name, "analog", f"sim/{flavor}/decks/dc_op.sp", dc_op)
            save_text_artifact_and_record(workflow_id, agent_name, "analog", f"sim/{flavor}/decks/dc_sweep_vin.sp", dc_sweep_vin)
            save_text_artifact_and_record(workflow_id, agent_name, "analog", f"sim/{flavor}/decks/ac_loopgain.sp", ac_loopgain)
            save_text_artifact_and_record(workflow_id, agent_name, "analog", f"sim/{flavor}/decks/ac_psrr.sp", ac_psrr)
            save_text_artifact_and_record(workflow_id, agent_name, "analog", f"sim/{flavor}/decks/tran_loadstep.sp", tran_loadstep)

        save_text_artifact_and_record(workflow_id, agent_name, "analog", "sim/parse/extract_metrics.py", _extract_metrics_py())

        # Provide an initial metrics.json placeholder so downstream summary/correlation doesn't crash
        metrics_stub = {
            "source": "sim",
            "confidence": "low",
            "notes": ["Initial stub metrics; generated before any real simulator parsing"],
            "metrics": {},
        }
        save_text_artifact_and_record(workflow_id, agent_name, "analog", "sim/results/metrics.json", json.dumps(metrics_stub, indent=2))

    return state
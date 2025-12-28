import os
import json
from datetime import datetime
from typing import Any, Dict, Optional, List

from utils.artifact_utils import save_text_artifact_and_record

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

client_openai = OpenAI() if OpenAI else None


def _log(log_path: str, msg: str) -> None:
    print(msg)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now().isoformat()}] {msg}\n")


def _find_spec_json(workflow_dir: str, state: dict) -> Optional[Dict[str, Any]]:
    if isinstance(state.get("spec_json"), dict):
        return state["spec_json"]

    p = state.get("spec_json")
    if isinstance(p, str) and p.endswith(".json") and os.path.exists(p):
        return json.load(open(p, "r", encoding="utf-8"))

    for root, _, files in os.walk(workflow_dir):
        for fn in files:
            if fn.endswith(".json") and "spec" in fn.lower():
                try:
                    return json.load(open(os.path.join(root, fn), "r", encoding="utf-8"))
                except Exception:
                    pass
    return None


def _default_sva_module(module_name: str = "chiploop_assertions") -> str:
    return f"""\
/*
 * Auto-generated SVA scaffold (generic).
 * Intended for simulation/regression. Customize per interface/protocol.
 */

module {module_name} (
  input logic clk,
  input logic rst_n
);

  // ----------------------------
  // Generic reset sanity checks
  // ----------------------------
  property p_no_x_on_reset_release;
    @(posedge clk) disable iff (!rst_n)
      !$isunknown(rst_n);
  endproperty

  a_no_x_on_reset_release: assert property(p_no_x_on_reset_release)
    else $error("Reset has X/Z around release.");

  // ----------------------------
  // Generic safety scaffold
  // (Add protocol properties here)
  // ----------------------------

endmodule
"""


def _maybe_llm_expand(spec: Dict[str, Any], sva: str, log_path: str) -> str:
    if not client_openai:
        return sva

    try:
        prompt = (
            "You are a senior RTL verification engineer.\n"
            "Expand this SVA scaffold using ONLY generic, industry-standard properties.\n"
            "Constraints:\n"
            "- Do NOT invent custom signal names.\n"
            "- If spec contains interface names/signals, you may reference them only if present verbatim.\n"
            "- Keep it synthesizable? Not required; SVA for simulation is fine.\n"
            "- Return SystemVerilog code only.\n\n"
            f"SPEC_JSON:\n{json.dumps(spec, indent=2)}\n\n"
            f"SVA_CODE:\n{sva}\n"
        )
        resp = client_openai.chat.completions.create(
            model=os.getenv("DIGITAL_SVA_MODEL", "gpt-4o-mini"),
            messages=[
                {"role": "system", "content": "Return code only. No markdown."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        _log(log_path, f"LLM expand skipped/failed: {e}")
        return sva


def run_agent(state: dict) -> dict:
    agent_name = "Assertions (SVA) Agent"
    print("\n🧪 Running Assertions (SVA) Agent...")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)
    os.makedirs("artifact", exist_ok=True)

    log_path = os.path.join("artifact", "digital_sva_assertions_agent.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("Assertions (SVA) Agent Log\n")

    spec = _find_spec_json(workflow_dir, state)
    if not spec:
        _log(log_path, "WARNING: No spec JSON found; generating generic SVA only.")
        spec = {}

    sva = _default_sva_module()
    sva = _maybe_llm_expand(spec, sva, log_path)

    save_text_artifact_and_record(workflow_id, agent_name, "digital", "chiploop_assertions.sv", sva)
    save_text_artifact_and_record(workflow_id, agent_name, "digital", "digital_sva_assertions_agent.log",
                                  open(log_path, "r", encoding="utf-8").read())

    state["sva_assertions_path"] = os.path.join(workflow_dir, "digital", "chiploop_assertions.sv")
    return state

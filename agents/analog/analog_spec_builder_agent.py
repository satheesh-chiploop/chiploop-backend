import os
import json
from utils.artifact_utils import save_text_artifact_and_record
from agents.analog._analog_llm import llm_text, safe_json_load


def run_agent(state: dict) -> dict:
    print("\n🔬 Running Analog Spec Builder Agent...")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")

    analog_dir = os.path.join(workflow_dir, "analog")
    spec_dir = os.path.join(analog_dir, "spec")

    os.makedirs(spec_dir, exist_ok=True)

    datasheet = (
        state.get("datasheet_text")
        or state.get("analog_datasheet")
        or state.get("analog_spec_text")
        or state.get("spec_text")
        or state.get("spec")
        or state.get("description")
        or ""
    ).strip()

  

    if not datasheet:
        raise ValueError("Analog datasheet not provided")

    print(f"[DEBUG] analog state keys = {list(state.keys())}")
    print(f"[DEBUG] datasheet_text present = {bool(state.get('datasheet_text'))}")
    print(f"[DEBUG] analog_datasheet present = {bool(state.get('analog_datasheet'))}")
    print(f"[DEBUG] spec present = {bool(state.get('spec'))}")
    print(f"[DEBUG] spec_text present = {bool(state.get('spec_text'))}")

    prompt = f"""
You are an analog design architect.

Convert the following analog datasheet into a STRICT normalized JSON contract.

Return JSON only.
Do not return markdown.
Do not return explanations.

REQUIRED TOP-LEVEL FIELDS:
- block_name
- module_name
- description
- operating_constraints
- ports
- functionality
- responsibilities
- must_drive
- must_receive
- must_not_drive
- reset_behavior
- behavior_rules
- behavioral_contract
- deliverables

STRICT FIELD RULES

1. block_name
- short canonical block identifier

2. module_name
- exact Verilog module name to be generated downstream

3. description
- short high-level block description

4. operating_constraints
Must be an object with:
- clock_domains: list
- reset_signals: list
- fixed_assumptions: list

Each clock_domains item should include when available:
- name
- frequency_mhz
- period_ns

Each reset_signals item should include when available:
- name
- active_low
- async

5. ports
- must be a list of objects
- every port object must include:
  - name
  - direction
  - width
- direction must be exactly one of:
  - input
  - output
  - inout
- width must be integer >= 1
- do not invent extra ports unless strictly required by the datasheet

6. functionality
- full behavioral summary of what the block does

7. responsibilities
- list of concrete responsibilities of the block

8. must_drive
- list of signals this block must drive

9. must_receive
- list of signals this block must consume

10. must_not_drive
- list of signals this block must never drive

11. reset_behavior
- textual description of reset behavior
- must explicitly describe output behavior during reset if reset exists

12. behavior_rules
- list of concrete behavioral rules
- include timing, pulse behavior, gating, update rules, determinism rules, and default behavior when relevant

13. behavioral_contract
Must be a structured object with these generic fields:
- deterministic_behavior: true/false
- state_elements: list
- input_to_output_relations: list
- event_triggers: list
- output_update_rules: list
- error_conditions: list
- latency_rules: list
- timing_rules: list

Use empty lists when information is not available.
Do NOT hardcode domain-specific keys like adc, dac, ldo, pll, sensor, etc.
Keep this schema generic.

14. deliverables
- list of expected downstream artifacts, for example:
  - behavioral_model
  - abstract_view_notes
  - params_json

STRICT RULES
- The JSON must be generic and reusable for any analog behavioral block.
- Do NOT emit domain-specific schema keys unless they are values inside generic lists/strings.
- Do NOT use ambiguous words like:
  - random
  - optional
  - stable
unless the datasheet explicitly requires them.
- If the datasheet is underspecified, choose the simplest deterministic contract.
- Preserve exact module naming intent from the datasheet where provided.
- The contract must be suitable for generating a compile-safe Verilog-2005 behavioral model.

Datasheet:
{datasheet}
"""

    raw = llm_text(prompt)
    spec = safe_json_load(raw)

    if not spec:
        raise RuntimeError("LLM failed to produce valid analog spec")


    block = (spec.get("block_name") or "analog_block").strip()
    module_name = (spec.get("module_name") or block).strip()

    # Canonical naming rule:
    # preserve explicit module_name from spec if present;
    # otherwise default module_name = block_name
    spec["block_name"] = block
    spec["module_name"] = module_name


    spec_path = os.path.join(spec_dir, "spec_normalized.json")

    with open(spec_path, "w") as f:
        json.dump(spec, f, indent=2)

    save_text_artifact_and_record(
        workflow_id,
        "Analog Spec Builder Agent",
        "analog/spec",
        "spec_normalized.json",
        json.dumps(spec, indent=2),
    )

    state["analog_spec"] = spec
    state["analog_block_name"] = block

    # Publish canonical analog module signature for downstream integration
    analog_signature = {
        spec["module_name"]: {
            "ports": spec.get("ports", [])
        }
    }

    state["analog_module_signature"] = analog_signature
    state["analog_signatures"] = analog_signature
    state["analog_rtl_signatures"] = analog_signature

    print(f"✅ Analog spec generated for {block}")

    return state
import os
import json
from portkey_ai import Portkey
from openai import OpenAI
from utils.artifact_utils import save_text_artifact_and_record

PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")
client_portkey = Portkey(api_key=PORTKEY_API_KEY)
client_openai = OpenAI()


def _read_json_if_exists(v):
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.endswith(".json") and os.path.exists(v):
        with open(v, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def _safe_dump(obj):
    try:
        return json.dumps(obj, indent=2)
    except Exception:
        return "null"


def _detect_spec_mode(spec_obj: dict) -> str:
    if not isinstance(spec_obj, dict):
        return "unknown"
    if isinstance(spec_obj.get("hierarchy"), dict):
        return "hierarchical"
    if spec_obj.get("name") and spec_obj.get("rtl_output_file"):
        return "flat"
    return "unknown"


def run_agent(state: dict) -> dict:
    print("\n🗺️ Running Digital Register Map Agent...")

    agent_name = "Digital Register Map Agent"
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)

    user_prompt = (state.get("spec", "") or "").strip()

    spec_obj = (
        _read_json_if_exists(state.get("digital_spec_json"))
        or _read_json_if_exists(state.get("spec_json"))
    )
    arch_obj = _read_json_if_exists(state.get("digital_architecture_json"))
    micro_obj = _read_json_if_exists(state.get("digital_microarchitecture_json"))

    if not spec_obj:
        state["status"] = "❌ Missing digital spec JSON for register map generation."
        return state

    spec_mode = _detect_spec_mode(spec_obj)

    prompt = f"""
You are a senior SoC register architect.

DIGITAL_SPEC_JSON is the primary source of truth.
ARCHITECTURE_JSON and MICROARCH_JSON are descriptive only.
If they conflict with DIGITAL_SPEC_JSON, DIGITAL_SPEC_JSON wins.

SPEC MODE:
{spec_mode}

INPUTS
USER_REQUEST:
{user_prompt}

DIGITAL_SPEC_JSON:
{_safe_dump(spec_obj)}

ARCHITECTURE_JSON:
{_safe_dump(arch_obj)}

MICROARCH_JSON:
{_safe_dump(micro_obj)}

OUTPUT RULES
- Output ONLY one raw JSON object.
- No markdown.
- No prose.
- No comments.

TASK
Generate a firmware-visible register map only if it is compatible with DIGITAL_SPEC_JSON.
Do NOT invent new hierarchy.
Do NOT invent incompatible top/module ports.
Do NOT force AXI/APB if not implied by the spec.
If the spec clearly implies an I2C/custom byte-register interface, prefer a custom 8-bit register bus description.
If a value wider than the data bus must be exposed, split it across multiple byte registers.
Define field-level semantics explicitly.

OUTPUT SCHEMA
{{
  "derived_from_spec_only": true,
  "spec_mode": "{spec_mode}",
  "regmap": {{
    "bus": "custom|i2c|abstract|minimal|axi_lite|apb",
    "base_address": "0x00",
    "addr_width": 8,
    "data_width": 8,
    "registers": [
      {{
        "name": "CONTROL",
        "offset": "0x01",
        "access": "RW",
        "description": "Control register",
        "fields": [
          {{"name": "ENABLE", "lsb": 0, "msb": 0, "access": "RW", "reset": 0, "description": "..."}}
        ]
      }}
    ]
  }},
  "interrupts": {{
    "sources": [
      {{"name": "ADC_DONE_IRQ", "description": "..."}},
      {{"name": "FAULT_IRQ", "description": "..."}}
    ]
  }},
  "software_driver_intent": {{
    "init_sequence": [],
    "polling_sequence": [],
    "irq_sequence": []
  }},
  "consistency_notes": [
    "All assumptions remain compatible with DIGITAL_SPEC_JSON."
  ]
}}
""".strip()

    try:
        completion = client_portkey.chat.completions.create(
            model="@chiploop/gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            stream=False,
        )
        llm_output = completion.choices[0].message.content or ""
    except Exception as e:
        state["status"] = f"❌ Register map LLM generation failed: {e}"
        return state

    raw_path = os.path.join(workflow_dir, "digital_regmap_raw_output.txt")
    with open(raw_path, "w", encoding="utf-8") as f:
        f.write(llm_output)

    try:
        regmap = json.loads(llm_output.strip())
    except Exception as e:
        regmap = {
            "error": "LLM JSON parse failed",
            "parse_error": str(e),
            "raw": llm_output.strip()
        }

    out_path = os.path.join(workflow_dir, "digital_regmap.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(regmap, f, indent=2)

    try:
        with open(raw_path, "r", encoding="utf-8") as f:
            save_text_artifact_and_record(
                workflow_id=workflow_id,
                agent_name=agent_name,
                subdir="digital",
                filename="digital_regmap_raw_output.txt",
                content=f.read(),
            )
        with open(out_path, "r", encoding="utf-8") as f:
            save_text_artifact_and_record(
                workflow_id=workflow_id,
                agent_name=agent_name,
                subdir="digital",
                filename="digital_regmap.json",
                content=f.read(),
            )
    except Exception as e:
        print(f"⚠️ Failed to upload regmap artifacts: {e}")

    rel_regmap_path = "digital/digital_regmap.json"

    digital = state.setdefault("digital", {})
    digital["regmap"] = regmap
    digital["digital_regmap"] = regmap
    digital["digital_regmap_path"] = rel_regmap_path
    digital["register_map_path"] = rel_regmap_path

    state.update({
        "status": "✅ Digital register map generated.",
        "digital_regmap": regmap,
        "digital_regmap_path": rel_regmap_path,
        "digital_register_map_path": rel_regmap_path,
        "digital_regmap_json": out_path,
        "workflow_id": workflow_id,
        "workflow_dir": workflow_dir,
    })
    return state 

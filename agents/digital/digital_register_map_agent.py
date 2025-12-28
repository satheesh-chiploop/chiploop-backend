import os
import json
from portkey_ai import Portkey
from openai import OpenAI

from utils.artifact_utils import save_text_artifact_and_record


PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")
client_portkey = Portkey(api_key=PORTKEY_API_KEY)
client_openai = OpenAI()


def _read_json_if_exists(path: str):
    if isinstance(path, str) and path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def _safe_dump(obj) -> str:
    try:
        return json.dumps(obj, indent=2)
    except Exception:
        return str(obj)


def run_agent(state: dict) -> dict:
    print("\n🗺️ Running Digital Register Map Agent...")

    agent_name = "Digital Register Map Agent"
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)

    user_prompt = (state.get("spec", "") or "").strip()
    spec_obj = _read_json_if_exists(state.get("spec_json"))
    arch_obj = _read_json_if_exists(state.get("digital_architecture_json"))
    micro_obj = _read_json_if_exists(state.get("digital_microarchitecture_json"))

    prompt = f"""
You are a senior SoC register/CSR architect.

INPUTS:
- USER_REQUEST (may be empty): {user_prompt}
- SPEC_JSON (may be null):
{_safe_dump(spec_obj)}
- ARCHITECTURE_JSON (may be null):
{_safe_dump(arch_obj)}
- MICROARCH_JSON (may be null):
{_safe_dump(micro_obj)}

OUTPUT RULES (CRITICAL):
- DO NOT use markdown.
- Output ONLY a single raw JSON object. No extra text.
- JSON must be valid (parseable by json.loads).
- No comments in JSON.

TASK:
Create an industry-standard register map suitable for an AXI-Lite/APB-style CSR block.
Keep it generic: define typical ID/VERSION, CONTROL, STATUS, INTR_STATUS, INTR_MASK,
and a small set of feature registers relevant to the described IP (but avoid random custom flags).

Output schema:
{{
  "regmap": {{
    "bus":"axi_lite|apb|custom",
    "base_address":"0x40000000",
    "addr_width": 32,
    "data_width": 32,
    "registers":[
      {{
        "name":"ID",
        "offset":"0x000",
        "access":"RO",
        "reset":"0x00000000",
        "desc":"...",
        "fields":[
          {{"name":"value","lsb":0,"msb":31,"access":"RO","reset":0,"desc":"..."}}
        ]
      }}
    ]
  }},
  "access_policy_notes":[
    "RW fields are synchronous to clk",
    "RO fields are status snapshots",
    "W1C for interrupt status where appropriate"
  ],
  "interrupts": {{
    "sources":[{{"name":"event0","desc":"...","sticky":true}}],
    "register_convention":"INTR_STATUS (W1C), INTR_MASK (RW), INTR_PENDING (RO optional)"
  }},
  "software_driver_intent": {{
    "init_sequence":["..."],
    "polling_sequence":["..."],
    "irq_sequence":["..."]
  }}
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

    regmap = None
    try:
        regmap = json.loads(llm_output.strip())
    except Exception as e:
        regmap = {"error": "LLM JSON parse failed", "parse_error": str(e), "raw": llm_output.strip()}

    out_path = os.path.join(workflow_dir, "digital_regmap.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(regmap, f, indent=2)

    try:
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="digital",
            filename="digital_regmap_raw_output.txt",
            content=open(raw_path, "r", encoding="utf-8").read(),
        )
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="digital",
            filename="digital_regmap.json",
            content=open(out_path, "r", encoding="utf-8").read(),
        )
    except Exception as e:
        print(f"⚠️ Failed to upload regmap artifacts: {e}")

    state.update({
        "status": "✅ Digital register map generated.",
        "digital_regmap_json": out_path,
        "workflow_id": workflow_id,
        "workflow_dir": workflow_dir,
    })
    return state

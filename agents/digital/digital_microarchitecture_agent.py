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
    if isinstance(v, str) and v and os.path.exists(v):
        try:
            with open(v, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def _safe_dump(obj) -> str:
    try:
        return json.dumps(obj, indent=2)
    except Exception:
        return str(obj)


def _normalize_spec(spec_obj: dict):
    if not isinstance(spec_obj, dict):
        raise ValueError("digital spec must be a JSON object")

    if isinstance(spec_obj.get("hierarchy"), dict):
        hier = spec_obj["hierarchy"]
        top = hier.get("top_module")
        modules = hier.get("modules", [])
        if not isinstance(top, dict) or not top.get("name"):
            raise ValueError("hierarchy.top_module.name missing")
        if not isinstance(modules, list):
            raise ValueError("hierarchy.modules must be a list")
        return {
            "spec_mode": "hierarchical",
            "top_module": top["name"],
            "modules": [top] + modules,
            "raw": spec_obj,
        }

    if spec_obj.get("name") and spec_obj.get("rtl_output_file"):
        return {
            "spec_mode": "flat",
            "top_module": spec_obj["name"],
            "modules": [spec_obj],
            "raw": spec_obj,
        }

    raise ValueError("Unsupported spec JSON format")


def run_agent(state: dict) -> dict:
    print("\n🧩 Running Digital Microarchitecture Agent...")

    agent_name = "Digital Microarchitecture Agent"
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)

    user_prompt = (state.get("spec", "") or "").strip()

    spec_obj = (
        _read_json_if_exists(state.get("digital_spec_json"))
        or _read_json_if_exists(state.get("spec_json"))
    )
    arch_obj = _read_json_if_exists(state.get("digital_architecture_json"))

    if not spec_obj:
        state["status"] = "❌ Missing digital spec JSON for microarchitecture generation."
        return state

    spec = _normalize_spec(spec_obj)

    prompt = f"""
You are a senior RTL/microarchitecture designer.

DIGITAL_SPEC_JSON is the single source of truth.
ARCHITECTURE_JSON is descriptive only.
Your task is to describe implementation intent only.

CRITICAL RULES
- Do NOT redefine architecture.
- Do NOT rename modules.
- Do NOT rename ports.
- Do NOT invent new modules.
- Do NOT invent hidden interfaces.
- Do NOT change filenames.
- This output is descriptive only.
- Do NOT invent complex FSMs unless clearly implied by the spec.
- Prefer the simplest implementation intent consistent with the spec.

INPUTS
USER_REQUEST:
{user_prompt}

DIGITAL_SPEC_JSON:
{_safe_dump(spec_obj)}

ARCHITECTURE_JSON:
{_safe_dump(arch_obj)}

OUTPUT RULES
- Output ONLY a single raw JSON object.
- No markdown.
- No prose before or after JSON.
- No comments.

If DIGITAL_SPEC_JSON is flat, output:
{{
  "spec_mode": "flat",
  "derived_from_spec_only": true,
  "module_name": "...",
  "microarchitecture_summary": {{
    "implementation_style": "...",
    "control_strategy": "...",
    "state_strategy": "...",
    "output_strategy": "..."
  }},
  "fsm_intent": {{
    "present": false,
    "description": "...",
    "states": []
  }},
  "datapath_intent": {{
    "inputs": [],
    "internal_storage": [],
    "output_mapping": []
  }},
  "sequencing_notes": [],
  "timing_notes": [],
  "ownership_notes": [],
  "consistency_notes": [
    "Microarchitecture is descriptive only.",
    "Interfaces remain exactly as defined in digital_spec_json."
  ]
}}

If DIGITAL_SPEC_JSON is hierarchical, output:
{{
  "spec_mode": "hierarchical",
  "derived_from_spec_only": true,
  "top_module": "...",
  "module_microarchitecture": [
    {{
      "name": "...",
      "implementation_style": "...",
      "control_strategy": "...",
      "state_strategy": "...",
      "output_strategy": "...",
      "ownership_notes": []
    }}
  ],
  "fsm_intent": [],
  "datapath_intent": [],
  "sequencing_notes": [],
  "timing_notes": [],
  "consistency_notes": [
    "Microarchitecture is descriptive only.",
    "No hierarchy, ports, or filenames may differ from digital_spec_json."
  ]
}}

Return JSON only.
""".strip()

    try:
        completion = client_portkey.chat.completions.create(
            model="@chiploop/gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            stream=False,
        )
        llm_output = completion.choices[0].message.content or ""
    except Exception as e:
        state["status"] = f"❌ Microarchitecture LLM generation failed: {e}"
        return state

    raw_path = os.path.join(workflow_dir, "digital_microarchitecture_raw_output.txt")
    with open(raw_path, "w", encoding="utf-8") as f:
        f.write(llm_output)

    try:
        micro = json.loads(llm_output.strip())
    except Exception as e:
        state["status"] = f"❌ Microarchitecture JSON parse failed: {e}"
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="digital",
            filename="digital_microarchitecture_llm_error.txt",
            content=llm_output,
        )
        return state

    out_path = os.path.join(workflow_dir, "digital_microarchitecture.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(micro, f, indent=2)

    try:
        with open(raw_path, "r", encoding="utf-8") as f:
            save_text_artifact_and_record(
                workflow_id=workflow_id,
                agent_name=agent_name,
                subdir="digital",
                filename="digital_microarchitecture_raw_output.txt",
                content=f.read(),
            )
        with open(out_path, "r", encoding="utf-8") as f:
            save_text_artifact_and_record(
                workflow_id=workflow_id,
                agent_name=agent_name,
                subdir="digital",
                filename="digital_microarchitecture.json",
                content=f.read(),
            )
    except Exception as e:
        print(f"⚠️ Failed to upload microarchitecture artifacts: {e}")

    state.update({
        "status": "✅ Digital microarchitecture generated.",
        "digital_microarchitecture_json": out_path,
        "digital_microarchitecture_path": out_path,
        "workflow_id": workflow_id,
        "workflow_dir": workflow_dir,
    })
    return state
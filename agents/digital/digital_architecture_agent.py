import os
import json
from portkey_ai import Portkey
from openai import OpenAI
from utils.artifact_utils import save_text_artifact_and_record

PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")
client_portkey = Portkey(api_key=PORTKEY_API_KEY)
client_openai = OpenAI()


def _safe_dump(obj) -> str:
    try:
        return json.dumps(obj, indent=2)
    except Exception:
        return str(obj)


def _read_json_if_exists(v):
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.endswith(".json") and os.path.exists(v):
        try:
            with open(v, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


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
            "top_ports": top.get("ports", []),
            "modules": [top] + modules,
            "inter_module_signals": spec_obj.get("inter_module_signals", []),
            "signal_ownership": spec_obj.get("signal_ownership", []),
            "raw": spec_obj,
        }

    if spec_obj.get("name") and spec_obj.get("rtl_output_file"):
        return {
            "spec_mode": "flat",
            "top_module": spec_obj["name"],
            "top_ports": spec_obj.get("ports", []),
            "modules": [spec_obj],
            "inter_module_signals": [],
            "signal_ownership": [],
            "raw": spec_obj,
        }

    raise ValueError("Unsupported spec JSON format")


def run_agent(state: dict) -> dict:
    print("\n🏗️ Running Digital Architecture Agent...")

    agent_name = "Digital Architecture Agent"
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)

    user_prompt = (state.get("spec", "") or "").strip()

    spec_obj = (
        _read_json_if_exists(state.get("digital_spec_json"))
        or _read_json_if_exists(state.get("spec_json"))
    )

    if not spec_obj:
        state["status"] = "❌ Missing digital spec JSON for architecture generation."
        return state

    spec = _normalize_spec(spec_obj)

    prompt = f"""
You are a senior digital hardware architect.

DIGITAL_SPEC_JSON is the single source of truth.
Your task is to generate a descriptive architecture document only.

CRITICAL RULES
- Do NOT redefine hierarchy.
- Do NOT rename modules.
- Do NOT rename ports.
- Do NOT invent new modules.
- Do NOT invent new ports.
- Do NOT change filenames.
- Do NOT become a second source of truth.
- This output is descriptive only.
- Preserve detailed module responsibilities from the spec.
- Preserve explicit interface summaries derived from inter-module contracts.

INPUTS
USER_REQUEST:
{user_prompt}

DIGITAL_SPEC_JSON:
{_safe_dump(spec_obj)}

OUTPUT RULES
- Output ONLY one raw JSON object.
- No markdown.
- No prose before or after JSON.
- No comments.

If spec mode is flat, output:
{{
  "spec_mode": "flat",
  "derived_from_spec_only": true,
  "top_module": "...",
  "design_summary": {{
    "purpose": "...",
    "operating_model": "...",
    "external_interfaces": []
  }},
  "module_architecture": {{
    "name": "...",
    "role": "...",
    "responsibilities": [],
    "interface_notes": [],
    "ownership_notes": []
  }},
  "data_flow_summary": [],
  "clock_reset_summary": {{
    "clocking": "...",
    "reset_behavior": "..."
  }},
  "integration_notes": [],
  "consistency_notes": [
    "This document is descriptive only.",
    "Hierarchy, ports, and filenames are inherited from digital_spec_json."
  ]
}}

If spec mode is hierarchical, output:
{{
  "spec_mode": "hierarchical",
  "derived_from_spec_only": true,
  "top_module": "...",
  "design_summary": {{
    "purpose": "...",
    "operating_model": "...",
    "external_interfaces": []
  }},
  "module_architecture": [
    {{
      "name": "...",
      "role": "...",
      "responsibilities": [],
      "interface_notes": [],
      "ownership_notes": []
    }}
  ],
  "interface_summary": [
    {{
      "from": "...",
      "to": "...",
      "signals": [],
      "intent": "..."
    }}
  ],
  "data_flow_summary": [],
  "clock_reset_summary": {{
    "clocking": "...",
    "reset_behavior": "..."
  }},
  "integration_notes": [],
  "consistency_notes": [
    "This document is descriptive only.",
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
        state["status"] = f"❌ Architecture LLM generation failed: {e}"
        return state

    raw_path = os.path.join(workflow_dir, "digital_architecture_raw_output.txt")
    with open(raw_path, "w", encoding="utf-8") as f:
        f.write(llm_output)

    try:
        arch = json.loads(llm_output.strip())
    except Exception as e:
        state["status"] = f"❌ Digital architecture JSON parse failed: {e}"
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="digital",
            filename="digital_architecture_llm_error.txt",
            content=llm_output,
        )
        return state

    out_path = os.path.join(workflow_dir, "digital_architecture.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(arch, f, indent=2)

    ports = []
    for p in spec["top_ports"]:
        if isinstance(p, dict) and p.get("name"):
            ports.append({
                "name": p["name"],
                "direction": p.get("direction", "input"),
                "width": int(p.get("width", 1) or 1),
            })

    digital_signature = {spec["top_module"]: {"ports": ports}}

    try:
        with open(raw_path, "r", encoding="utf-8") as f:
            save_text_artifact_and_record(
                workflow_id=workflow_id,
                agent_name=agent_name,
                subdir="digital",
                filename="digital_architecture_raw_output.txt",
                content=f.read(),
            )
        with open(out_path, "r", encoding="utf-8") as f:
            save_text_artifact_and_record(
                workflow_id=workflow_id,
                agent_name=agent_name,
                subdir="digital",
                filename="digital_architecture.json",
                content=f.read(),
            )
    except Exception as e:
        print(f"⚠️ Failed to upload architecture artifacts: {e}")

    state.update({
        "status": "✅ Digital architecture generated.",
        "digital_architecture_json": out_path,
        "digital_architecture_path": out_path,
        "workflow_id": workflow_id,
        "workflow_dir": workflow_dir,
        "digital_module_signature": digital_signature,
        "digital_rtl_signatures": digital_signature,
        "rtl_signatures": digital_signature,
    })
    return state

    
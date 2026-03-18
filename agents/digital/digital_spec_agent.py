import os
import json
from utils.artifact_utils import save_text_artifact_and_record
from portkey_ai import Portkey
from openai import OpenAI

PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")
client_portkey = Portkey(api_key=PORTKEY_API_KEY)
client_openai = OpenAI()


def _normalize_spec_json(spec_json: dict) -> dict:
    """
    Supports BOTH:
    1) Flat single-module form:
       {
         "name": "...",
         "ports": [...],
         "functionality": "...",
         "rtl_output_file": "x.v"
       }

    2) Hierarchical form:
       {
         "design_name": "...",
         "hierarchy": {
           "top_module": {...},
           "modules": [...]
         }
       }

    Returns:
    - normalized JSON
    - mode: "flat" or "hierarchical"
    """
    if not isinstance(spec_json, dict):
        raise ValueError("Spec JSON must be a dictionary.")

    # Hierarchical form
    if isinstance(spec_json.get("hierarchy"), dict):
        hier = spec_json["hierarchy"]
        top = hier.get("top_module")
        modules = hier.get("modules", [])

        if not isinstance(top, dict):
            raise ValueError("hierarchy.top_module must be an object.")
        if not top.get("name"):
            raise ValueError("hierarchy.top_module.name is required.")
        if not top.get("rtl_output_file"):
            raise ValueError("hierarchy.top_module.rtl_output_file is required.")
        if not isinstance(modules, list):
            raise ValueError("hierarchy.modules must be a list.")

        norm = {
            "design_name": spec_json.get("design_name") or top["name"],
            "hierarchy": {
                "top_module": top,
                "modules": modules,
            },
            "inter_module_signals": spec_json.get("inter_module_signals", []),
            "signal_ownership": spec_json.get("signal_ownership", []),
        }
        return norm, "hierarchical"

    # Flat single-module form
    if spec_json.get("name") and spec_json.get("rtl_output_file"):
        norm = {
            "name": spec_json["name"],
            "description": spec_json.get("description", ""),
            "ports": spec_json.get("ports", []),
            "functionality": spec_json.get("functionality", ""),
            "rtl_output_file": spec_json["rtl_output_file"],
        }
        return norm, "flat"

    raise ValueError("Spec JSON must be either flat single-module form or hierarchical form.")


def _validate_port(port: dict, where: str) -> None:
    if not isinstance(port, dict):
        raise ValueError(f"{where} must be an object.")
    if not port.get("name"):
        raise ValueError(f"{where}.name is required.")
    if port.get("direction") not in ("input", "output", "inout"):
        raise ValueError(f"{where}.direction must be input/output/inout.")
    width = port.get("width", 1)
    if not isinstance(width, int) or width < 1:
        raise ValueError(f"{where}.width must be integer >= 1.")


def _validate_module(mod: dict, where: str) -> None:
    if not isinstance(mod, dict):
        raise ValueError(f"{where} must be an object.")
    if not mod.get("name"):
        raise ValueError(f"{where}.name is required.")
    if not mod.get("rtl_output_file"):
        raise ValueError(f"{where}.rtl_output_file is required.")
    ports = mod.get("ports")
    if not isinstance(ports, list):
        raise ValueError(f"{where}.ports must be a list.")
    for i, p in enumerate(ports):
        _validate_port(p, f"{where}.ports[{i}]")


def _validate_spec_contract(spec_json: dict, mode: str) -> None:
    if mode == "flat":
        _validate_module(spec_json, "spec")
        return

    hier = spec_json["hierarchy"]
    top = hier["top_module"]
    modules = hier.get("modules", [])

    _validate_module(top, "hierarchy.top_module")

    seen_mods = set()
    seen_files = set()

    def check_unique(mod: dict, where: str):
        name = mod["name"]
        rtl_file = mod["rtl_output_file"]
        if name in seen_mods:
            raise ValueError(f"Duplicate module name detected: {name}")
        if rtl_file in seen_files:
            raise ValueError(f"Duplicate rtl_output_file detected: {rtl_file}")
        seen_mods.add(name)
        seen_files.add(rtl_file)
        _validate_module(mod, where)

    check_unique(top, "hierarchy.top_module")
    for idx, mod in enumerate(modules):
        check_unique(mod, f"hierarchy.modules[{idx}]")


def run_agent(state: dict) -> dict:
    print("\n🚀 Running Digital Spec Agent (contract-only mode)...")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)

    user_prompt = (
        state.get("spec")
        or state.get("digital_spec")
        or state.get("digital_spec_text")
        or state.get("soc_spec")
        or state.get("system_spec")
        or state.get("description")
        or ""
    ).strip()

    if not user_prompt:
        state["status"] = "❌ No spec provided"
        return state

    prompt = f"""
USER DIGITAL SPECIFICATION:
{user_prompt}

You are a professional ASIC digital architect.

Your task is to generate ONLY the authoritative digital design contract as JSON.
Do NOT generate RTL.
Do NOT generate Verilog.
Do NOT include markdown.
Do NOT include prose before or after JSON.

STRICT OUTPUT RULES
- Output ONLY one raw JSON object.
- No markdown fences.
- JSON must parse with json.loads().

IMPORTANT
You may output EITHER of these two valid forms.

VALID FORM A — Flat single-module form:
{{
  "name": "module_name",
  "description": "Explain purpose.",
  "ports": [
    {{"name": "clk", "direction": "input", "width": 1, "type": "wire"}},
    {{"name": "reset_n", "direction": "input", "width": 1, "active_low": true}},
    {{"name": "enable", "direction": "input", "width": 1}},
    {{"name": "count", "direction": "output", "width": 4, "type": "reg"}}
  ],
  "functionality": "Describe logic.",
  "rtl_output_file": "module_name.v"
}}

VALID FORM B — Hierarchical multi-module form:
{{
  "design_name": "top_module_name",
  "hierarchy": {{
    "top_module": {{
      "name": "top_module_name",
      "description": "Describe top-level integration.",
      "ports": [
        {{"name": "clk", "direction": "input", "width": 1}},
        {{"name": "reset_n", "direction": "input", "width": 1, "active_low": true}},
        {{"name": "result", "direction": "output", "width": 8}}
      ],
      "functionality": "Describe top-level behavior and integration.",
      "rtl_output_file": "top_module_name.v"
    }},
    "modules": [
      {{
        "name": "sub_module_a",
        "description": "Purpose of submodule.",
        "ports": [
          {{"name": "a", "direction": "input", "width": 8}},
          {{"name": "b", "direction": "input", "width": 8}},
          {{"name": "y", "direction": "output", "width": 8}}
        ],
        "functionality": "Logic role of submodule.",
        "rtl_output_file": "sub_module_a.v"
      }}
    ]
  }},
  "inter_module_signals": [
    {{
      "name": "sig_a_to_b",
      "width": 8,
      "source": "sub_module_a.y",
      "destinations": ["sub_module_b.a"]
    }}
  ],
  "signal_ownership": [
    {{
      "signal": "result",
      "owner": "top_module_name.result"
    }}
  ]
}}

RULES
- If the design is truly just one module, output the flat single-module form.
- If the design has internal hierarchy, output the hierarchical form.
- Define exact module names.
- Define exact ports.
- Define exact rtl_output_file names.
- Every port must include name, direction, width.
- direction must be input/output/inout.
- width must be integer >= 1.
- If the user spec is incomplete, choose the simplest valid architecture ONCE and encode it here.
- This JSON becomes the source of truth for downstream agents.

Return JSON only.
""".strip()

    try:
        print("🌐 Calling LLM via Portkey...")
        completion = client_portkey.chat.completions.create(
            model="@chiploop/gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            stream=False,
        )
        llm_output = completion.choices[0].message.content or ""
        print("✅ Response received.")
    except Exception as e:
        print(f"❌ LLM generation failed: {e}")
        state["status"] = f"❌ LLM generation failed: {e}"
        return state

    raw_output_path = os.path.join(workflow_dir, "llm_raw_output.txt")
    with open(raw_output_path, "w", encoding="utf-8") as rf:
        rf.write(llm_output)

    try:
        parsed_json = json.loads(llm_output.strip())
        spec_json, mode = _normalize_spec_json(parsed_json)
        _validate_spec_contract(spec_json, mode)
        print(f"✅ Spec JSON parsed and validated successfully. mode={mode}")
    except Exception as e:
        state["status"] = f"❌ JSON parse/normalize failed: {e}"
        raise ValueError(f"LLM JSON parse/normalize failed: {e}")

    if mode == "flat":
        module_name = spec_json["name"]
    else:
        module_name = spec_json["hierarchy"]["top_module"]["name"]

    spec_json_path = os.path.join(workflow_dir, f"{module_name}_spec.json")
    with open(spec_json_path, "w", encoding="utf-8") as sf:
        json.dump(spec_json, sf, indent=2)

    log_path = os.path.join(workflow_dir, "spec_agent_contract.log")
    with open(log_path, "w", encoding="utf-8") as lf:
        lf.write("Digital Spec Agent completed successfully.\n")
        lf.write("Mode: contract-only\n")
        lf.write(f"Spec mode: {mode}\n")
        lf.write(f"Spec JSON: {spec_json_path}\n")
        if mode == "flat":
            lf.write(f"Module: {spec_json['name']}\n")
            lf.write(f"RTL file: {spec_json['rtl_output_file']}\n")
        else:
            top_obj = spec_json["hierarchy"]["top_module"]
            lf.write(f"Top module: {top_obj['name']}\n")
            lf.write(f"Top RTL file: {top_obj['rtl_output_file']}\n")
            lf.write(f"Submodule count: {len(spec_json['hierarchy'].get('modules', []))}\n")

    try:
        agent_name = "Digital Spec Agent"

        with open(raw_output_path, "r", encoding="utf-8") as f:
            save_text_artifact_and_record(
                workflow_id=workflow_id,
                agent_name=agent_name,
                subdir="spec",
                filename="llm_raw_output.txt",
                content=f.read(),
            )

        with open(spec_json_path, "r", encoding="utf-8") as f:
            save_text_artifact_and_record(
                workflow_id=workflow_id,
                agent_name=agent_name,
                subdir="spec",
                filename=os.path.basename(spec_json_path),
                content=f.read(),
            )

        with open(log_path, "r", encoding="utf-8") as f:
            save_text_artifact_and_record(
                workflow_id=workflow_id,
                agent_name=agent_name,
                subdir="spec",
                filename="spec_agent_contract.log",
                content=f.read(),
            )
    except Exception as e:
        print(f"⚠️ Spec Agent artifact upload failed: {e}")

    state.update({
        "status": "✅ Digital spec contract generated.",
        "artifact": spec_json_path,
        "artifact_list": [spec_json_path],
        "artifact_log": log_path,
        "spec_json": spec_json_path,
        "digital_spec_json": spec_json_path,
        "workflow_dir": workflow_dir,
        "workflow_id": workflow_id,
    })

    return state


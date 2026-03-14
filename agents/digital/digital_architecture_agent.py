import os
import json
import datetime
import re
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


def run_agent(state: dict) -> dict:
    print("\n🏗️ Running Digital Architecture Agent...")

    agent_name = "Digital Architecture Agent"
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)

    # Inputs: prefer structured spec if already created
    spec_json_path = state.get("spec_json")
    user_prompt = (state.get("spec", "") or "").strip()

    spec_obj = None
    if spec_json_path and isinstance(spec_json_path, str) and os.path.exists(spec_json_path):
        try:
            with open(spec_json_path, "r", encoding="utf-8") as f:
                spec_obj = json.load(f)
        except Exception:
            spec_obj = None

    # Build LLM prompt
    prompt = f"""
You are a senior digital hardware architect.

INPUTS:
- USER_REQUEST (may be empty): {user_prompt}

- EXISTING_SPEC_JSON (may be null):
{_safe_dump(spec_obj)}

OUTPUT RULES (CRITICAL):
- DO NOT use markdown.
- Output ONLY a single raw JSON object. No extra text.
- JSON must be valid (parseable by json.loads).
- Do NOT include comments in JSON.

TASK:
Generate a block-level architecture for the digital IP described by the inputs.

Output schema:
{{
  "design_name": "string",
  "summary": "1-3 sentences",
  "assumptions": ["..."],
  "interfaces": {{
    "clocks": [{{"name":"clk","notes":"..."}}],
    "resets": [{{"name":"reset_n","active_low": true, "notes":"..."}}],
    "external_ports": [{{"name":"...", "dir":"input|output|inout", "width": 1, "notes":"..."}}],
    "bus_interfaces": [{{"type":"axi_lite|apb|custom", "role":"slave|master", "notes":"..."}}]
  }},
  "architecture": {{
    "blocks": [
      {{
        "name":"block_name",
        "type":"datapath|control|interface|storage|clocking|other",
        "responsibility":"...",
        "inputs":["..."],
        "outputs":["..."],
        "key_signals":["..."]
      }}
    ],
    "data_paths": ["..."],
    "control_paths": ["..."],
    "clock_domains": [{{"domain":"...", "signals":["clk"], "notes":"..."}}],
    "reset_strategy": "..."
  }},
  "performance_area_tradeoffs": {{
    "latency_cycles":"string",
    "throughput":"string",
    "area_drivers":["..."],
    "power_drivers":["..."]
  }},
  "verification_hooks": {{
    "observability_signals":["..."],
    "assertions_recommended":["..."],
    "coverage_points":["..."]
  }}
}}
""".strip()

    # Call LLM
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

    # Save raw output
    raw_path = os.path.join(workflow_dir, "digital_architecture_raw_output.txt")
    with open(raw_path, "w", encoding="utf-8") as f:
        f.write(llm_output)

    # Parse JSON
    arch = None
    parse_err = None
    try:
        import re

        json_match = re.search(r"\{.*\}", llm_output, re.S)
        if not json_match:
            state["status"] = "❌ Digital architecture JSON not found in LLM output"
            return state

        arch = json.loads(json_match.group(0))

    except Exception as e:
        parse_err = str(e)
        state["status"] = f"❌ Digital architecture JSON parse failed: {parse_err}"

        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="digital",
            filename="digital_architecture_llm_error.txt",
            content=llm_output,
        )

        return state

        
        

    # Save JSON file
    out_path = os.path.join(workflow_dir, "digital_architecture.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(arch, f, indent=2)

    # Upload artifacts
    try:
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="digital",
            filename="digital_architecture_raw_output.txt",
            content=open(raw_path, "r", encoding="utf-8").read(),
        )
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="digital",
            filename="digital_architecture.json",
            content=open(out_path, "r", encoding="utf-8").read(),
        )
    except Exception as e:
        print(f"⚠️ Failed to upload architecture artifacts: {e}")

    # Build canonical digital module signature for downstream integration
    design_name = arch.get("design_name") or "digital_block"

    ports = []

    interfaces = arch.get("interfaces", {}) or {}

    for clk in interfaces.get("clocks", []) or []:
        if isinstance(clk, dict) and clk.get("name"):
            ports.append({
            "name": clk["name"],
            "direction": "input",
            "width": 1,
            })

    for rst in interfaces.get("resets", []) or []:
        if isinstance(rst, dict) and rst.get("name"):
            ports.append({
                "name": rst["name"],
                "direction": "input",
                "width": 1,
            })

    for p in interfaces.get("external_ports", []) or []:
        if isinstance(p, dict) and p.get("name"):
            ports.append({
                "name": p["name"],
                "direction": p.get("dir") or p.get("direction") or "input",
                "width": int(p.get("width", 1) or 1),
            })

    digital_signature = {
        design_name: {
            "ports": ports
        }
    }

    state.update({
        "status": "✅ Digital architecture generated.",
        "digital_architecture_json": out_path,
        "workflow_id": workflow_id,
        "workflow_dir": workflow_dir,
        "digital_module_signature": digital_signature,
        "digital_rtl_signatures": digital_signature,
        "rtl_signatures": digital_signature,
    })

    return state

    
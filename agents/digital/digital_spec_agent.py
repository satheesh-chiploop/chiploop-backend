import subprocess
import os
import datetime
import json
import requests
import re

from utils.artifact_utils import upload_artifact_generic, append_artifact_record
from portkey_ai import Portkey
from openai import OpenAI

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
USE_LOCAL_OLLAMA = os.getenv("USE_LOCAL_OLLAMA", "false").lower() == "true"
PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")
client_portkey = Portkey(
    api_key=PORTKEY_API_KEY
)

client_openai = OpenAI()


# --- ✅ Added cleanup function (minimal addition) ---
def cleanup_verilog(verilog_code: str) -> str:
    lines = verilog_code.splitlines()
    seen = set()
    cleaned = []
    for line in lines:
        stripped = line.strip()
        # skip duplicate signal declarations (clk, reset, etc.)
        if any(keyword in stripped for keyword in ["input", "output", "inout"]):
            tokens = stripped.replace(";", "").split()
            sigs = [t for t in tokens if t not in ["input", "output", "inout", "wire", "reg", "logic"]]
            if any(sig in seen for sig in sigs):
                continue
            for sig in sigs:
                seen.add(sig)
        cleaned.append(line)
    return "\n".join(cleaned)


# ... keep imports and setup as-is (Portkey client etc.)

def run_agent(state: dict) -> dict:
    print("\n🚀 Running Spec Agent v5 (LLM JSON-first + RTL)...")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)

    user_prompt = state.get("spec", "")
    if not user_prompt:
        state["status"] = "❌ No spec provided"
        return state

    # ✅ NEW: unified prompt (LLM decides everything; JSON is authoritative)
    prompt = f"""
You are a professional digital design engineer.

You will produce output in this exact order:
1) A JSON object or array that fully describes all modules in the hierarchy.
   - If the design contains multiple modules, return a top-level object:
       {
         "design_name": "top_module_name",
         "hierarchy": {
            "modules": "submodules here",
            "top_module": "integration details here"
         }
       }
   - Each module entry must contain:
       { "name", "description", "ports", "functionality", "rtl_output_file" }
   - "top_module" should include "submodules" array with instance and connection details.
   - module_name: string (exact module name to use in RTL)
   - hierarchy_role: "top" | "submodule"
   - description: string (one paragraph)
   - ports: array of objects, each: 
       {{ "name": string, "direction": "input"|"output"|"inout", "width": int (>=1), 
          "type": "wire"|"reg"|null, "active_low": bool|null, "kind": "clock"|"reset"|"data"|null, 
          "description": string|null }}
   - clock: OPTIONAL array of clock domain objects (if applicable):
       [{{ "name": string, "frequency_mhz": number|null, "duty_cycle": number|null }}]
   - reset: OPTIONAL array of reset objects (if applicable):
       [{{ "name": string, "active_low": bool|null, "sync": bool|null, "duration_cycles": int|null }}]
   - parameters: OPTIONAL array of parameter objects:
       [{{ "name": string, "type": "int"|"logic"|"bit"|"string"|null, "default": any|null, "description": string|null }}]
   - constraints: OPTIONAL object (e.g., timing/reset/protocol notes)
   - rationale: brief string explaining naming and ports as derived from user text
   - examples: OPTIONAL object with small usage or testbench hints
   IMPORTANT:
   - Respect EXACT user-provided names and structure if present.
   - Do NOT add default ports (like clk or reset) unless clearly implied or explicitly requested by the user.
   - widths are integers (1 means scalar).
   - Keep field names exactly as above.

2) The Verilog-2005 implementation, delimited with the exact markers:
---BEGIN VERILOG---
<full synthesizable Verilog-2005 code here>
---END VERILOG---

Rules for Verilog:
- Must be synthesizable Verilog-2005 (no SystemVerilog).
- File/module name must match JSON.module_name exactly.
- Start with 'module' and end with 'endmodule'. No markdown fences.
- Declare all ports explicitly in the module port list; avoid duplicates.
- Use only defined signals; terminate statements; one 'endmodule'.
- No commentary or prose outside JSON and the delimited Verilog section.

User request:
{user_prompt}
""".strip()

    rtl_code = ""
    spec_json = {}
    try:
        print("🌐 Using Portkey/OpenAI backend..for rtl code generation start.")
        completion = client_portkey.chat.completions.create(
            model="@chiploop/gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            stream=False,
        )
        llm_output = completion.choices[0].message.content or ""
        print("✅ Portkey response received successfully.")
    except Exception as e:
        print(f"❌ Portkey call failed: {repr(e)}")
        state["status"] = f"❌ LLM generation failed: {e}"
        return state
    print("🌐 Using Portkey/OpenAI backend..for rtl code generation end.")

    # ✅ Split JSON and Verilog blocks
    try:
        if "---BEGIN VERILOG---" in llm_output:
            spec_part, rtl_part = llm_output.split("---BEGIN VERILOG---", 1)
            rtl_code = rtl_part.split("---END VERILOG---")[0].strip()
        else:
            spec_part = llm_output
            rtl_code = ""
        spec_json = json.loads(spec_part.strip())
    except Exception as e:
        print(f"⚠️ Failed to parse LLM JSON; capturing raw: {e}")
        spec_json = {"description": "LLM JSON parse failed", "raw": spec_part.strip()}
    
    # ✅ Determine module_name from JSON first; fallback to Verilog header; final fallback 'auto_module'
    module_name = spec_json.get("module_name")
    if not module_name and rtl_code:
        import re
        m = re.search(r"module\s+([A-Za-z_][A-Za-z0-9_]*)", rtl_code)
        module_name = m.group(1) if m else None
    if not module_name:
        module_name = "auto_module"

    # ✅ Write spec JSON and RTL
    spec_json_path = os.path.join(workflow_dir, f"{module_name}_spec.json")
    with open(spec_json_path, "w", encoding="utf-8") as f:
        json.dump(spec_json, f, indent=2)

    # strip accidental fences and sanitize
    rtl_code = (rtl_code or "").replace("```verilog", "").replace("```", "").strip()
    if "module" in rtl_code:
        rtl_code = rtl_code[rtl_code.index("module"):]

    os.makedirs(workflow_dir, exist_ok=True)
    if "hierarchy" in spec_json:
        hierarchy = spec_json["hierarchy"]
        all_modules = []
        if "modules" in hierarchy:
            for m in hierarchy["modules"]:
                fname = m.get("rtl_output_file", f"{m['name']}.v")
                code = m.get("rtl_code") or ""
                fpath = os.path.join(workflow_dir, fname)
                with open(fpath, "w") as f:
                   f.write(code)
                   all_modules.append(fpath)
        if "top_module" in hierarchy:
            top = hierarchy["top_module"]
            fname = top.get("rtl_output_file", f"{top['name']}.v")
            code = top.get("rtl_code") or ""
            fpath = os.path.join(workflow_dir, fname)
            with open(fpath, "w") as f:
               f.write(code)
            all_modules.append(fpath)
        state["artifact_list"] = all_modules
    else:
    # single-module fallback
        verilog_file = os.path.join(workflow_dir, f"{module_name}.v")
        with open(verilog_file, "w") as f:
            f.write(rtl_code)
        state["artifact"] = verilog_file

    # quick syntax check (same as before)
    log_path = os.path.join(workflow_dir, "spec_agent_compile.log")
    try:
        compile_status = "⚠️ Skipped syntax check (hierarchical design)."
        if "hierarchy" not in spec_json:
            result = subprocess.run(
               ["/usr/bin/iverilog", "-o", "design.out", verilog_file],
               check=True, capture_output=True, text=True
            )
            compile_status = "✅ Verilog syntax check passed."
            state["status"] = "✅ RTL and spec.json generated successfully"
    except subprocess.CalledProcessError as e:
        compile_status = "⚠️ RTL generated but failed compilation"
        state["status"] = compile_status
        state["error_log"] = (e.stderr or e.stdout or "").strip()

    with open(log_path, "w") as logf:
        logf.write(f"Spec processed at {datetime.datetime.now()}\n")
        logf.write(f"Module: {module_name}\n")
        logf.write(f"{compile_status}\n")

    # upload artifacts (unchanged)
    try:
        user_id = state.get("user_id", "anonymous")
        workflow_id = state.get("workflow_id", "default")
        artifact_list = state.get("artifact_list", [])
        if artifact_list:
            for f in artifact_list:
              append_artifact_record(workflow_id, "spec_agent_output", f)
    except Exception as e:
          print(f"⚠️ Artifact append failed: {e}")
    except Exception as e:
        print(f"⚠️ Artifact upload failed: {e}")

    print(f"\n✅ Generated {verilog_file} and {spec_json_path}")
    # Instead of adding hierarchical=True or new columns, just mark in state
    state["hierarchical_mode"] = "true" if "hierarchy" in spec_json else "false"

# Keep artifact_list for downstream
    state["artifact_list"] = all_modules if "hierarchy" in spec_json else [verilog_file]
  
    state.update({
        "artifact": verilog_file,
        "artifact_log": log_path,
        "spec_json": spec_json_path,
        "rtl": rtl_code,
        "workflow_dir": workflow_dir,
        "workflow_id": workflow_id,
    })
    return state



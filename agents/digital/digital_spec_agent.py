import os
import re
import json
import subprocess
import datetime
from utils.artifact_utils import append_artifact_record
from portkey_ai import Portkey
from openai import OpenAI

# ---------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------
PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")
client_portkey = Portkey(api_key=PORTKEY_API_KEY)
client_openai = OpenAI()


# ---------------------------------------------------------------------
# Core Agent
# ---------------------------------------------------------------------
def run_agent(state: dict) -> dict:
    print("\n🚀 Running Digital Spec Agent (final stable build)...")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)

    user_prompt = state.get("spec", "").strip()
    if not user_prompt:
        state["status"] = "❌ No spec provided"
        return state

    # -----------------------------------------------------------------
    # 1️⃣ Build LLM Prompt  (User first, then structured format)
    # -----------------------------------------------------------------
    prompt = f"""
USER DESIGN REQUEST:
{user_prompt}

---

You are a professional ASIC RTL design engineer.

🔒 IMPORTANT OUTPUT FORMAT RULES
- DO NOT use markdown code fences (no ```json, no ```verilog).
- DO NOT include comments inside JSON (no //, no #, no text after commas).
- DO NOT include explanations, headers, or extra text.
- ONLY produce raw JSON followed immediately by the Verilog code markers.
- JSON must be 100% valid (parseable by json.loads in Python).
- JSON must be the **first output**, and Verilog must start only after JSON ends.

---

Generate two outputs in this strict order:

1️⃣ **JSON SPECIFICATION**

   Output a JSON object describing all modules and hierarchy. Use this schema:

   - Hierarchical (multiple modules):
     {{
       "design_name": "top_module_name",
       "hierarchy": {{
         "modules": [
           {{
             "name": "sub_module_a",
             "description": "Purpose of submodule.",
             "ports": [
                {{"name": "a", "direction": "input", "width": 8}},
                {{"name": "b", "direction": "input", "width": 8}},
                {{"name": "y", "direction": "output", "width": 8}}
             ],
             "functionality": "Logic description.",
             "rtl_output_file": "sub_module_a.v"
           }}
         ],
         "top_module": {{
           "name": "top_module_name",
           "description": "Describe top-level integration.",
           "ports": [
             {{"name": "clk", "direction": "input", "width": 1}},
             {{"name": "reset_n", "direction": "input", "width": 1, "active_low": true}},
             {{"name": "result", "direction": "output", "width": 8}}
           ],
           "functionality": "Describe how submodules are connected.",
           "rtl_output_file": "top_module_name.v"
         }}
       }}
     }}

   - Flat (single module):
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

---

2️⃣ **VERILOG CODE**
Immediately after the JSON, output the complete synthesizable Verilog-2005 implementation
for all modules, enclosed using these exact delimiters: for each module , user these markers as below

---BEGIN VERILOG---
<module Verilog code here>
---END VERILOG---

🧠 Guidelines:
- JSON first, Verilog second.
- Do NOT wrap JSON or Verilog in triple backticks or markdown blocks.
- Every module must include name, ports, functionality, rtl_output_file.
- Use clean, compact JSON (no comments, no ```json).
- Each module must appear in its own BEGIN/END block delimiters
""".strip()
    # -----------------------------------------------------------------
    # 2️⃣ LLM Call
    # -----------------------------------------------------------------
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

    # -----------------------------------------------------------------
    # 3️⃣ Save Raw Output
    # -----------------------------------------------------------------
    raw_output_path = os.path.join(workflow_dir, "llm_raw_output.txt")
    with open(raw_output_path, "w", encoding="utf-8") as rf:
        rf.write(llm_output)
    print(f"📄 Saved raw LLM output to {raw_output_path}")

    # -----------------------------------------------------------------
    # 4️⃣ Extract JSON
    # -----------------------------------------------------------------
    spec_part = llm_output.split("---BEGIN", 1)[0].strip()
    try:
        spec_json = json.loads(spec_part)
        print("✅ JSON parsed successfully.")
    except Exception as e:
        print(f"⚠️ JSON parse failed: {e}")
        spec_json = {"description": "LLM JSON parse failed", "raw": spec_part}

    # -----------------------------------------------------------------
    # 5️⃣ Extract Verilog
    # -----------------------------------------------------------------
    verilog_blocks = re.findall(
        r"---BEGIN\s+([\w\-.]+)---(.*?)---END\s+\1---",
        llm_output,
        re.DOTALL,
    )
    verilog_map = {fname.strip(): code.strip() for fname, code in verilog_blocks}

    # If generic VERILOG block is present, extract and split modules inside
    if (not verilog_map) or (list(verilog_map.keys()) == ["VERILOG"]):
        generic_match = re.search(
            r"---BEGIN\s+VERILOG---(.*?)---END\s+VERILOG---",
            llm_output,
            re.DOTALL,
        )
        if generic_match:
            flat_code = generic_match.group(1).strip()

            # ✅ Extract each module definition
            module_defs = re.findall(
                r"(?s)(module\s+(\w+)\b.*?endmodule)",
                flat_code,
            )
            
            
            if module_defs:
                print(f"🧩 Detected combined VERILOG block — splitting into {len(module_defs)} modules.")
                verilog_map = {f"{mname}.v": code.strip() for code, mname in module_defs}
            else:
                # fallback if only one module exists
                print("🧩 Single module detected in VERILOG block.")
                verilog_map = {"auto_module.v": flat_code}
        else:
            print("⚠️ No VERILOG markers found at all.")
    # -----------------------------------------------------------------
    # 6️⃣ Auto-Flatten for simple cases
    # -----------------------------------------------------------------
    if "hierarchy" in spec_json and isinstance(spec_json["hierarchy"], dict):
        h = spec_json["hierarchy"]
        modules = h.get("modules", [])
        top = h.get("top_module")

        if isinstance(top, str):
            print("🔧 Flattening string top_module → single-module spec.")
            spec_json = {
                "name": spec_json.get("design_name", "auto_module"),
                "description": top,
                "rtl_output_file": f"{spec_json.get('design_name', 'auto_module')}.v",
            }
        elif not modules and isinstance(top, dict):
            print("🔧 Flattening single top_module hierarchy.")
            spec_json = top
        elif len(modules) == 1:
            print("🔧 Flattening single sub-module hierarchy.")
            spec_json = modules[0]

    # -----------------------------------------------------------------
    # 7️⃣ Determine module name
    # -----------------------------------------------------------------
    module_name = (
        spec_json.get("name")
        or spec_json.get("module_name")
        or spec_json.get("design_name")
        or "auto_module"
    )

    # -----------------------------------------------------------------
    # 8️⃣ Save spec JSON
    # -----------------------------------------------------------------
    spec_json_path = os.path.join(workflow_dir, f"{module_name}_spec.json")
    with open(spec_json_path, "w", encoding="utf-8") as sf:
        json.dump(spec_json, sf, indent=2)
    print(f"✅ Saved structured spec JSON → {spec_json_path}")

    # -----------------------------------------------------------------
    # 9️⃣ Write Verilog file(s)
    # -----------------------------------------------------------------
    # -----------------------------------------------------------------
# 9️⃣ Write Verilog file(s)
# -----------------------------------------------------------------
    all_modules = []
    if verilog_map:
        print(f"🧱 Writing {len(verilog_map)} Verilog module(s).")
        verilog_file = None  # track top separately
        for fname, code in verilog_map.items():
            fpath = os.path.join(workflow_dir, fname)
            with open(fpath, "w", encoding="utf-8") as vf:
              vf.write(code)
            print(f"✅ Wrote {len(code)} chars to {fname}")
            all_modules.append(fpath)
        # Identify top module file automatically
            if "top" in fname.lower() or "system" in fname.lower():
              verilog_file = fpath
    # fallback if no top identified
        if not verilog_file:
            verilog_file = all_modules[-1]

    else:
        print("⚠️ No Verilog found, writing empty stub.")
        verilog_file = os.path.join(workflow_dir, f"{module_name}.v")
        open(verilog_file, "w").close()
        all_modules.append(verilog_file)


    # -----------------------------------------------------------------
    # 🔟 Syntax check
    # -----------------------------------------------------------------
    log_path = os.path.join(workflow_dir, "spec_agent_compile.log")
    compile_status = "✅ Generated successfully."
    try:
        subprocess.run(
            ["/usr/bin/iverilog", "-o", "temp.out", verilog_file],
            check=True, capture_output=True, text=True
        )
    except subprocess.CalledProcessError as e:
        compile_status = "⚠️ RTL generated but syntax check failed."
        with open(log_path, "w") as lf:
            lf.write(e.stderr or e.stdout or "")
        print("⚠️ Verilog syntax check failed.")
    else:
        with open(log_path, "w") as lf:
            lf.write("Verilog syntax check passed.\n")

    # -----------------------------------------------------------------
    # 11️⃣ Record artifacts
    # -----------------------------------------------------------------
    try:
        for f in all_modules:
            append_artifact_record(workflow_id, "spec_agent_output", f)
        append_artifact_record(workflow_id, "spec_agent_json", spec_json_path)
        append_artifact_record(workflow_id, "spec_agent_log", log_path)
        append_artifact_record(workflow_id, "llm_raw_output", raw_output_path)
    except Exception as e:
        print(f"⚠️ Artifact append failed: {e}")

    # -----------------------------------------------------------------
    # 12️⃣ Finalize state
    # -----------------------------------------------------------------
    state.update({
        "status": compile_status,
        "artifact": verilog_file,
        "artifact_list": all_modules,
        "artifact_log": log_path,
        "spec_json": spec_json_path,
        "workflow_dir": workflow_dir,
        "workflow_id": workflow_id,
    })

    print(f"✅ Completed Spec Agent for workflow {workflow_id}")
    return state



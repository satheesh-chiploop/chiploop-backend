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
client_portkey = Portkey(api_key=PORTKEY_API_KEY)
client_openai = OpenAI()


def cleanup_verilog(verilog_code: str) -> str:
    lines = verilog_code.splitlines()
    seen = set()
    cleaned = []
    for line in lines:
        stripped = line.strip()
        if any(keyword in stripped for keyword in ["input", "output", "inout"]):
            tokens = stripped.replace(";", "").split()
            sigs = [t for t in tokens if t not in ["input", "output", "inout", "wire", "reg", "logic"]]
            if any(sig in seen for sig in sigs):
                continue
            for sig in sigs:
                seen.add(sig)
        cleaned.append(line)
    return "\n".join(cleaned)


def run_agent(state: dict) -> dict:
    print("\n🚀 Running Spec Agent (LLM JSON-first + RTL)...")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)

    user_prompt = state.get("spec", "")
    if not user_prompt:
        state["status"] = "❌ No spec provided"
        return state

    prompt = f"""
You are a professional digital design engineer.

USER DESIGN REQUEST:
{user_prompt}

You will produce output in this exact order:
1) A JSON object or array that fully describes all modules.
   - If the design contains multiple modules, return a top-level object:
       {{
         "design_name": "top_module_name",
         "hierarchy": {{
            "modules": "submodules here",
            "top_module": "integration details here"
         }}
       }}
   - Each module entry must contain:
       {{ "name", "description", "ports", "functionality", "rtl_output_file" }}
2) Immediately after the JSON, output the Verilog-2005 implementation.
   IMPORTANT: It must be delimited EXACTLY as shown below (these markers are mandatory):

   ---BEGIN VERILOG---
   <full synthesizable Verilog-2005 code here>
   ---END VERILOG---

   Do not omit these delimiters. Do not include any text or explanation outside these blocks.
""".strip()

    try:
        print("🌐 Calling Portkey/OpenAI backend...")
        completion = client_portkey.chat.completions.create(
            model="@chiploop/gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            stream=False,
        )
        llm_output = completion.choices[0].message.content or ""
        print("✅ Portkey response received.")
    except Exception as e:
        print(f"❌ Portkey call failed: {repr(e)}")
        state["status"] = f"❌ LLM generation failed: {e}"
        return state

    # 🧾 Save full LLM response for inspection
    raw_output_path = os.path.join(workflow_dir, "llm_raw_output.txt")
    with open(raw_output_path, "w", encoding="utf-8") as rawf:
        rawf.write(llm_output)
    print(f"📄 Saved full LLM output to {raw_output_path}")

    # ✅ Parse JSON before use
    spec_part = llm_output.split("---BEGIN", 1)[0].strip()
    try:
        spec_json = json.loads(spec_part)
        print("✅ JSON parsed successfully.")
    except Exception as e:
        print(f"⚠️ JSON parse failed: {e}")
        spec_json = {"description": "LLM JSON parse failed", "raw": spec_part}

    # ✅ Extract Verilog blocks (named or generic)
    verilog_blocks = re.findall(
        r"---BEGIN\s+([\w\-.]+)---(.*?)---END\s+\1---", llm_output, re.DOTALL
    )

    if not verilog_blocks:
        generic_blocks = re.findall(
            r"---BEGIN\s+VERILOG---(.*?)---END\s+VERILOG---",
            llm_output,
            re.DOTALL,
        )
        if generic_blocks:
            verilog_blocks = [("default.v", generic_blocks[0])]
            print("🧩 Captured generic VERILOG block.")
        else:
            print("⚠️ No explicit Verilog markers found in LLM output.")

    verilog_map = {fname.strip(): code.strip() for fname, code in verilog_blocks}

    # ✅ Auto-flatten logic
    if "hierarchy" in spec_json and isinstance(spec_json["hierarchy"], dict):
        h = spec_json["hierarchy"]
        modules = h.get("modules", [])
        top = h.get("top_module", {})
        if not modules and top:
            print("🔧 Auto-flattening hierarchy with only top_module.")
            spec_json = top
        elif len(modules) == 1 or (
            top.get("name") and modules and top.get("name") == modules[0].get("name")
        ):
            print("🔧 Auto-flattening single/redundant hierarchy.")
            spec_json = modules[0]

    # ✅ Naming fix
    module_name = (
        spec_json.get("name")
        or spec_json.get("module_name")
        or spec_json.get("design_name")
        or "auto_module"
    )

    spec_json_path = os.path.join(workflow_dir, f"{module_name}_spec.json")
    with open(spec_json_path, "w", encoding="utf-8") as f:
        json.dump(spec_json, f, indent=2)

    all_modules, verilog_file = [], None

    # --- Hierarchical handling ---
    if "hierarchy" in spec_json:
        print("🧱 Detected hierarchical design.")
        for m in spec_json["hierarchy"].get("modules", []):
            mname = m.get("name", "unnamed_module")
            fname = m.get("rtl_output_file", f"{mname}.v")
            code = (m.get("rtl_code") or verilog_map.get(fname, "") or "").strip()
            fpath = os.path.join(workflow_dir, fname)
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(code)
            all_modules.append(fpath)
            print(f"✅ Wrote {len(code)} chars to {fname}")
        top = spec_json["hierarchy"].get("top_module", {})
        if top:
            tname = top.get("name", "top_module")
            fname = top.get("rtl_output_file", f"{tname}.v")
            code = (top.get("rtl_code") or verilog_map.get(fname, "") or "").strip()
            fpath = os.path.join(workflow_dir, fname)
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(code)
            all_modules.append(fpath)
            print(f"✅ Wrote {len(code)} chars to {fname}")
        verilog_file = all_modules[-1] if all_modules else os.path.join(workflow_dir, "top.v")
        state["artifact_list"] = all_modules

    # --- Flat handling ---
    else:
        print("📄 Detected flat design.")
        flat_code = ""
        if verilog_map:
            _, flat_code = next(iter(verilog_map.items()))
        elif spec_json.get("rtl_code"):
            flat_code = spec_json["rtl_code"]

        verilog_file = os.path.join(workflow_dir, f"{module_name}.v")
        with open(verilog_file, "w", encoding="utf-8") as f:
            f.write(flat_code)
        print(f"✅ Wrote {len(flat_code)} chars to {verilog_file}")
        state["artifact"] = verilog_file
        all_modules = [verilog_file]

    # --- Syntax check (flat only) ---
    log_path = os.path.join(workflow_dir, "spec_agent_compile.log")
    try:
        if "hierarchy" not in spec_json:
            subprocess.run(["/usr/bin/iverilog", "-o", "design.out", verilog_file],
                           check=True, capture_output=True, text=True)
            compile_status = "✅ Verilog syntax check passed."
        else:
            compile_status = "⚙️ Skipped syntax check (hierarchical)."
    except subprocess.CalledProcessError as e:
        compile_status = "⚠️ RTL generated but failed compilation"
        state["error_log"] = e.stderr or e.stdout or ""
    state["status"] = compile_status

    with open(log_path, "w") as logf:
        logf.write(f"Spec processed at {datetime.datetime.now()}\n")
        logf.write(f"Module: {module_name}\n")
        logf.write(f"{compile_status}\n")

    # --- Upload artifacts (unchanged) ---
    try:
        for f in all_modules:
            append_artifact_record(workflow_id, "spec_agent_output", f)
        append_artifact_record(workflow_id, "spec_agent_log", log_path)
        append_artifact_record(workflow_id, "spec_agent_report", spec_json_path)
    except Exception as e:
        print(f"⚠️ Artifact append failed: {e}")

    state.update({
        "artifact": verilog_file,
        "artifact_list": all_modules,
        "artifact_log": log_path,
        "spec_json": spec_json_path,
        "workflow_dir": workflow_dir,
        "workflow_id": workflow_id,
        "hierarchical_mode": "true" if "hierarchy" in spec_json else "false",
    })
    print(f"✅ Completed Spec Agent for workflow {workflow_id}")
    return state



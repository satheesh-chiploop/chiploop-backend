import os
import re
import json
import subprocess
import datetime
from utils.artifact_utils import save_text_artifact_and_record
from portkey_ai import Portkey
from openai import OpenAI

# ---------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------
PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")
client_portkey = Portkey(api_key=PORTKEY_API_KEY)
client_openai = OpenAI()

def _normalize_spec_json(spec_json: dict) -> dict:
    """
    Accepts either:
      1) flat single-module JSON
      2) hierarchical JSON
    Returns canonical hierarchical form:
    {
      "design_name": "...",
      "hierarchy": {
        "top_module": {...},
        "modules": [...]
      }
    }
    """
    if not isinstance(spec_json, dict):
        raise ValueError("Spec JSON must be a dictionary.")

    # Already hierarchical
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

        return {
            "design_name": spec_json.get("design_name") or top["name"],
            "hierarchy": {
                "top_module": top,
                "modules": modules,
            },
        }

    # Flat single-module form
    if spec_json.get("name") and spec_json.get("rtl_output_file"):
        flat = spec_json
        return {
            "design_name": flat["name"],
            "hierarchy": {
                "top_module": flat,
                "modules": [],
            },
        }

    raise ValueError("Spec JSON must be either flat or hierarchical.")


def _collect_expected_rtl_files(spec_json: dict) -> set:
    hier = spec_json["hierarchy"]
    expected = set()

    top = hier.get("top_module", {})
    if top.get("rtl_output_file"):
        expected.add(top["rtl_output_file"])

    for m in hier.get("modules", []):
        if isinstance(m, dict) and m.get("rtl_output_file"):
            expected.add(m["rtl_output_file"])

    return expected
# ---------------------------------------------------------------------
# Core Agent
# ---------------------------------------------------------------------
def run_agent(state: dict) -> dict:
    print("\n🚀 Running Digital Spec Agent (final stable build)...")

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

    # -----------------------------------------------------------------
    # 1️⃣ Build LLM Prompt  (User first, then structured format)
    # -----------------------------------------------------------------
    prompt = f"""
USER DIGITAL SPECIFICATION:
{user_prompt}

---

You are a professional ASIC RTL design engineer.

The user specification above is the source of truth.
Do NOT override it with your own architecture unless the spec is missing details.
When details are missing, choose the simplest synthesizable implementation consistent with the user spec.

STRICT OUTPUT RULES
- Output valid JSON first, then Verilog.
- No markdown fences.
- No explanations before, between, or after outputs.
- JSON must parse with json.loads().
- Verilog must be synthesizable Verilog-2005.
- Do not emit placeholder modules.
- Do not emit empty modules.
- Do not reference undeclared signals.
- All instantiated ports must be declared and connected.
- All outputs must be driven.

Generate two outputs in this strict order:

1) JSON SPECIFICATION

Supported JSON forms:

- Hierarchical (multiple modules):
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
      "functionality": "Describe how submodules are connected.",
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
        "functionality": "Logic description.",
        "rtl_output_file": "sub_module_a.v"
      }}
    ]
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

JSON RULES
- Every module must include name, ports, functionality, rtl_output_file.
- hierarchy.top_module must be an object, never a string.
- rtl_output_file must exactly match the emitted filename.
- JSON must reflect the emitted RTL exactly.

2) VERILOG CODE

Emit one named block per file using these exact markers:

---BEGIN <filename>.v---
<verilog code>
---END <filename>.v---

Example:
---BEGIN digital_subsystem.v---
module digital_subsystem(...);
...
endmodule
---END digital_subsystem.v---

RTL QUALITY RULES
- Preserve the user spec structure.
- If the spec is hierarchical, keep the hierarchy.
- Do not invent extra top-level modules not implied by the spec.
- Keep logic minimal and deterministic.
- Prefer simple synthesizable logic over protocol-complete complexity unless explicitly requested.

RTL IMPLEMENTATION QUALITY RULES

Generate real synthesizable logic for every module.
Do not generate empty shells, stubs, TODOs, or placeholder comments such as:
- "logic goes here"
- "implement later"
- "placeholder"

Every module must:
- contain executable RTL logic
- drive all outputs
- declare all internal signals it uses
- avoid undeclared identifiers
- compile cleanly

For leaf modules, implement the simplest valid behavior consistent with the spec.

Minimum expectation:
- register/control modules must contain actual read/write or state-holding logic
- control modules must contain sequential and/or combinational logic that drives outputs
- interrupt modules must contain logic that derives irq from status inputs

A module with only ports and comments is invalid. Regenerate until all modules contain real logic.

PLACEHOLDER RTL IS FORBIDDEN

Do not use placeholder expressions or comment-based pseudo-code inside assignments or conditions.
The following are invalid and must never appear in the output:
- reg <= /* ... */;
- wire = /* ... */;
- if (/* ... */)
- case (/* ... */)
- comments used in place of executable logic

If exact protocol behavior is not specified, implement the simplest deterministic synthesizable behavior instead.
Use constants, simple counters, simple state bits, or pass-through logic rather than placeholders.
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
        parsed_json = json.loads(spec_part)
        spec_json = _normalize_spec_json(parsed_json)
        print("✅ JSON parsed and normalized successfully.")
    except Exception as e:
        state["status"] = f"❌ JSON parse/normalize failed: {e}"
        raise ValueError(f"LLM JSON parse/normalize failed: {e}")

    hier = spec_json["hierarchy"]
    top_obj = hier["top_module"]
    module_name = spec_json.get("design_name") or top_obj["name"]
    top_rtl_file = top_obj["rtl_output_file"]
    expected_rtl_files = _collect_expected_rtl_files(spec_json)


    verilog_blocks = re.findall(
        r"---BEGIN\s+([A-Za-z_][\w\-]*\.v)---(.*?)---END\s+\1---",
        llm_output,
        re.DOTALL,
    )
    verilog_map = {fname.strip(): code.strip() for fname, code in verilog_blocks}

    if not verilog_map:
        state["status"] = "❌ No named Verilog file blocks found in LLM output."
        raise ValueError(
            "Expected named Verilog blocks: "
            "---BEGIN <filename>.v--- ... ---END <filename>.v---"
        )

    all_modules = []
    verilog_file = None

    print(f"🧱 Writing {len(verilog_map)} Verilog module(s).")
    for fname, code in verilog_map.items():
        fpath = os.path.join(workflow_dir, fname)
        with open(fpath, "w", encoding="utf-8") as vf:
            vf.write(code)
        print(f"✅ Wrote {len(code)} chars to {fname}")
        all_modules.append(fpath)

        if fname == top_rtl_file:
            verilog_file = fpath

    actual_rtl_files = set(os.path.basename(p) for p in all_modules)
    missing_files = sorted(expected_rtl_files - actual_rtl_files)
    extra_files = sorted(actual_rtl_files - expected_rtl_files)

    if missing_files:
        raise ValueError(f"LLM failed to emit declared RTL files: {missing_files}")

    if extra_files:
        print(f"⚠️ Extra RTL files emitted by LLM: {extra_files}")

    if not verilog_file:
        raise ValueError(
            f"Top RTL file '{top_rtl_file}' declared in JSON was not emitted by the LLM."
        )


    # -----------------------------------------------------------------
    # 8️⃣ Save spec JSON
    # -----------------------------------------------------------------
    spec_json_path = os.path.join(workflow_dir, f"{module_name}_spec.json")
    with open(spec_json_path, "w", encoding="utf-8") as sf:
        json.dump(spec_json, sf, indent=2)
    print(f"✅ Saved structured spec JSON → {spec_json_path}")

        # -----------------------------------------------------------------
    # 🔟 Syntax check
    # -----------------------------------------------------------------
    log_path = os.path.join(workflow_dir, "spec_agent_compile.log")
    compile_status = "✅ Generated successfully."

    try:
        # include all generated .v files for hierarchical designs
        iverilog = os.getenv("IVERILOG_BIN", "/usr/bin/iverilog")
        compile_cmd = [iverilog, "-o", "temp.out"] + all_modules
        print(f"🧩 Running syntax check: {' '.join(os.path.basename(f) for f in compile_cmd[3:])}")

        subprocess.run(
            compile_cmd,
            check=True,
            capture_output=True,
            text=True
        )

        with open(log_path, "w") as lf:
            lf.write("Verilog syntax check passed.\n")

    except subprocess.CalledProcessError as e:
        compile_status = "⚠️ RTL generated but syntax check failed."
        with open(log_path, "w") as lf:
            lf.write(e.stderr or e.stdout or "")
        print("⚠️ Verilog syntax check failed.")
    # -----------------------------------------------------------------
    # 11️⃣ Record artifacts
    # -----------------------------------------------------------------
    # -----------------------------------------------------------------
    # 11️⃣ Upload artifacts to Supabase Storage + record JSON
    # -----------------------------------------------------------------
    try:
        agent_name = "Digital Spec Agent"

        # 11.1 LLM raw output
        try:
            with open(raw_output_path, "r", encoding="utf-8") as f:
                raw_content = f.read()
            save_text_artifact_and_record(
                workflow_id=workflow_id,
                agent_name=agent_name,
                subdir="spec",
                filename="llm_raw_output.txt",
                content=raw_content,
            )
        except Exception as e:
            print(f"⚠️ Failed to upload raw LLM output artifact: {e}")

        # 11.2 Spec JSON
        try:
            with open(spec_json_path, "r", encoding="utf-8") as f:
                spec_content = f.read()
            save_text_artifact_and_record(
                workflow_id=workflow_id,
                agent_name=agent_name,
                subdir="spec",
                filename=os.path.basename(spec_json_path),
                content=spec_content,
            )
        except Exception as e:
            print(f"⚠️ Failed to upload spec JSON artifact: {e}")

        # 11.3 Compile log
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                log_content = f.read()
            save_text_artifact_and_record(
                workflow_id=workflow_id,
                agent_name=agent_name,
                subdir="spec",
                filename="spec_agent_compile.log",
                content=log_content,
            )
        except Exception as e:
            print(f"⚠️ Failed to upload spec agent compile log artifact: {e}")

        # 11.4 Verilog modules (each .v file)
        for fpath in all_modules:
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    v_content = f.read()
                save_text_artifact_and_record(
                    workflow_id=workflow_id,
                    agent_name=agent_name,
                    subdir="spec",
                    filename=os.path.basename(fpath),
                    content=v_content,
                )
            except Exception as e:
                print(f"⚠️ Failed to upload Verilog artifact {fpath}: {e}")

        print("🧩 Spec Agent artifacts uploaded successfully.")

    except Exception as e:
        print(f"⚠️ Spec Agent artifact upload failed: {e}")


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
    state["digital_spec_json"] = spec_json_path

    print(f"✅ Completed Spec Agent for workflow {workflow_id}")
    return state



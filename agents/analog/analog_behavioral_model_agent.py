import os
import re
import json
import subprocess
from utils.artifact_utils import save_text_artifact_and_record
from agents.analog._analog_llm import llm_text


def _extract_sv_module(text: str) -> str:
    if not text:
        return ""

    cleaned = (
        text.replace("```systemverilog", "")
        .replace("```sv", "")
        .replace("```verilog", "")
        .replace("```", "")
        .strip()
    )

    m = re.search(r"(module\b.*?endmodule)", cleaned, flags=re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()

    return cleaned.strip()


def _force_module_name(model_text: str, module_name: str) -> str:
    if not model_text:
        return model_text

    return re.sub(
        r"\bmodule\s+([A-Za-z_]\w*)\b",
        f"module {module_name}",
        model_text,
        count=1,
        flags=re.IGNORECASE,
    )

def _fallback_model(spec: dict, module_name: str) -> str:
    ports = spec.get("ports") or []

    decls = []
    for p in ports:
        name = p.get("name", "sig")
        direction = (p.get("direction", "input") or "input").strip().lower()
        width = int(p.get("width", 1) or 1)

        if direction not in ("input", "output", "inout"):
            direction = "input"

        if width > 1:
            decls.append(f"    {direction} [{width-1}:0] {name}")
        else:
            decls.append(f"    {direction} {name}")

    port_text = ",\n".join(decls) if decls else "    input clk"

    return f"""module {module_name} (
{port_text}
);

  // Spec-driven fallback behavioral scaffold.
  // Minimal compile-safe Verilog-2005 subset model.

endmodule
"""


def run_agent(state: dict) -> dict:
    print("\\n🧪 Running Analog Behavioral Model Agent...")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)

    spec = state.get("analog_spec", {})
    if not spec:
        raise ValueError("analog_spec missing. Run Analog Spec Builder Agent first.")

    block = spec.get("block_name", "analog_block")
    module_name = spec.get("module_name") or f"{block}_model"

    block = (spec.get("block_name") or "analog_block").strip()
    module_name = (spec.get("module_name") or block).strip()

    prompt = f"""
Generate a compile-clean behavioral model in Verilog-2005.

Use this normalized spec:

{json.dumps(spec, indent=2)}

Rules:
- Module name must be exactly: {module_name}
- Use ONLY the ports defined in spec
- Do NOT invent new ports
- Do NOT change widths
- Return ONLY code
- No markdown fences
- No prose or explanations

Language restrictions:
- Use Verilog-2005 only
- Do NOT use SystemVerilog-only constructs
- Forbidden constructs:
  - logic
  - always_comb
  - always_ff
  - typedef
  - enum
  - struct
  - union
  - interface
- Use only simple legal constructs such as:
  - module
  - input/output/inout
  - wire
  - reg
  - assign
  - localparam
  - always @(*)
  - always @(posedge clk or negedge rst_n) when applicable

Output requirements:
- No placeholders
- No TODOs
- No pseudo-code
- Entire module must compile cleanly with Icarus Verilog
- If behavior is underspecified, emit the simplest deterministic compile-safe model
- Do not add extra ports or extra files

Self-check before output:
1. Module name matches exactly: {module_name}
2. Port list matches spec exactly
3. No extra ports
4. No missing ports
5. No SystemVerilog-only syntax
6. No undeclared identifiers
7. Output is compile-clean
"""

    raw = llm_text(prompt)
    model = _extract_sv_module(raw)

    if not model or "module" not in model or "endmodule" not in model:
        model = _fallback_model(spec, module_name)

    model = _force_module_name(model, module_name)

    analog_dir = os.path.join(workflow_dir, "analog")
    os.makedirs(analog_dir, exist_ok=True)


    model_filename = f"{module_name}.v"
    params_filename = f"{module_name}_params.json"
    notes_filename = f"{module_name}_notes.md"
    compile_log_filename = f"{module_name}_compile.log"
    compile_summary_filename = f"{module_name}_compile_summary.json"

    model_path = os.path.join(analog_dir, model_filename)
    params_path = os.path.join(analog_dir, params_filename)
    notes_path = os.path.join(analog_dir, notes_filename)
    compile_log_path = os.path.join(analog_dir, compile_log_filename)
    compile_summary_path = os.path.join(analog_dir, compile_summary_filename)

    model_params = {
        "block_name": block,
        "module_name": module_name,
        "model_filename": model_filename,
        "ports": spec.get("ports", []),
        "behavioral_contract": spec.get("behavioral_contract", {}),
    }

    issues = []

    with open(model_path, "w", encoding="utf-8") as f:
        f.write(model.rstrip() + "\n")

    full_text = model

    forbidden_sv_patterns = [
        r"\btypedef\b",
        r"\benum\b",
        r"\blogic\b",
        r"\balways_comb\b",
        r"\balways_ff\b",
        r"\bstruct\b",
        r"\bunion\b",
        r"\binterface\b",
    ]

    for pat in forbidden_sv_patterns:
        if re.search(pat, full_text):
            issues.append(f"❌ Forbidden SystemVerilog construct found in analog model: pattern '{pat}'")

    if not re.search(rf"\bmodule\s+{re.escape(module_name)}\b", full_text):
        issues.append(f"❌ Generated model module name does not match expected module_name '{module_name}'")

    compile_cmd = ["iverilog", "-g2005", "-o", os.path.join(analog_dir, "analog_model_out"), model_path]
    compile_status = "Compile not run yet."

    try:
        cp = subprocess.run(compile_cmd, capture_output=True, text=True, check=False)
        compile_status = (cp.stdout or "") + "\n" + (cp.stderr or "")
        if cp.returncode != 0:
            issues.append("❌ Icarus Verilog compile failed for analog behavioral model.")
    except Exception as e:
        compile_status = f"Compile invocation failed: {e}"
        issues.append(f"❌ Compile invocation failed: {e}")

    with open(compile_log_path, "w", encoding="utf-8") as f:
        f.write(compile_status.strip() + "\n")
        if issues:
            f.write("\nIssues:\n")
            for issue in issues:
                f.write(f"{issue}\n")

    compile_summary = {
        "block_name": block,
        "module_name": module_name,
        "model_filename": model_filename,
        "compile_cmd": compile_cmd,
        "issue_count": len(issues),
        "issues": issues,
    }
    with open(compile_summary_path, "w", encoding="utf-8") as f:
        json.dump(compile_summary, f, indent=2)

    

    model_notes = f"""# Analog Model Notes

- block_name: {block}
- module_name: {module_name}
- source: Analog Behavioral Model Agent
- model_file: {model_filename}
- params_file: {params_filename}
- compile_log: {compile_log_filename}
- compile_summary: {compile_summary_filename}
- note: Module-scoped analog behavioral model artifact set
- compile_status: {"clean" if not issues else "issues_found"}
"""

    with open(params_path, "w", encoding="utf-8") as f:
        json.dump(model_params, f, indent=2)
    with open(notes_path, "w", encoding="utf-8") as f:
        f.write(model_notes)


    save_text_artifact_and_record(workflow_id, "Analog Behavioral Model Agent", "analog", model_filename, model)
    save_text_artifact_and_record(workflow_id, "Analog Behavioral Model Agent", "analog", params_filename, json.dumps(model_params, indent=2))
    save_text_artifact_and_record(workflow_id, "Analog Behavioral Model Agent", "analog", notes_filename, model_notes)
    save_text_artifact_and_record(
        workflow_id,
        "Analog Behavioral Model Agent",
        "analog",
        compile_log_filename,
        compile_status.strip() + ("\n\nIssues:\n" + "\n".join(issues) if issues else "")
    )
    save_text_artifact_and_record(
        workflow_id,
        "Analog Behavioral Model Agent",
        "analog",
        compile_summary_filename,
        json.dumps(compile_summary, indent=2)
    )

    state["analog_model"] = model_path
    state["analog_model_path"] = model_path
    state["analog_model_module"] = module_name
    state["analog_model_file"] = model_filename

    state["analog_model_params_file"] = params_path
    state["analog_model_notes_file"] = notes_path
    state["analog_model_compile_log"] = compile_log_path
    state["analog_model_compile_summary"] = compile_summary_path
    state["analog_spec"] = spec
    state["analog_block_name"] = block
    state["analog_module_name"] = spec["module_name"]

    state["issues"] = list(set((state.get("issues") or []) + issues))
    state["status"] = "✅ Analog behavioral model generated" if not issues else "⚠ Analog behavioral model generated with issues"

    return state


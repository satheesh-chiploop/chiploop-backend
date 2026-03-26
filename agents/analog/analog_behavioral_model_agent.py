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

def _build_repair_prompt(
    original_prompt: str,
    spec: dict,
    module_name: str,
    broken_model: str,
    failure_text: str,
) -> str:
    return f"""
{original_prompt}

==============================
REPAIR MODE (PASS2)
==============================

Your previous RTL did not pass compile/lint.

Return ONLY corrected Verilog code.
Do not return markdown.
Do not return explanations.

SOURCE OF TRUTH
- The normalized spec JSON below is the ONLY source of truth.
- Preserve the interface exactly as defined in the spec JSON.
- Do NOT derive ports, widths, directions, or module name from the broken RTL.
- If the broken RTL conflicts with the spec JSON, the spec JSON wins.

NORMALIZED SPEC JSON:
{json.dumps(spec, indent=2)}

EXPECTED MODULE NAME:
{module_name}

BROKEN RTL:
{broken_model}

FAILURE LOG:
{failure_text}

PASS2 REPAIR RULES
- Preserve the same module name exactly as required by the spec JSON
- Preserve the same port list exactly as required by the spec JSON
- Do NOT add extra ports
- Do NOT remove ports
- Do NOT rename ports
- Do NOT change widths
- Do NOT change directions
- Fix only compile/lint correctness problems
- Use Verilog-2005 only
- If a signal is procedurally assigned, it must be declared as reg
- Remove multiple drivers
- Add default assignments in combinational logic
- Add default case branches where needed
- Ensure all declared outputs are driven
- Ensure meaningful declared inputs are used when required by the spec JSON
- Keep behavior deterministic and simple
- Do NOT redesign the block unless strictly required to fix compile/lint errors
- The repaired RTL is still wrong if any reported compile error or fatal lint issue remains

FINAL PASS2 SELF-CHECK
1. Module name matches spec JSON exactly
2. Port names match spec JSON exactly
3. Port directions match spec JSON exactly
4. Port widths match spec JSON exactly
5. No extra ports
6. No missing ports
7. No SystemVerilog-only syntax
8. No procedural assignment to wire-style outputs
9. No multiple drivers
10. RTL is compile/lint repair-focused only

Return corrected code only.
""".strip()



def _run_verilator_lint(model_path: str) -> tuple[int, str]:
    cmd = ["verilator", "--lint-only", model_path]
    cp = subprocess.run(cmd, capture_output=True, text=True, check=False)
    text = (cp.stdout or "") + "\n" + (cp.stderr or "")
    return cp.returncode, text


def _is_fatal_verilator_log(log_text: str) -> bool:
    fatal_tokens = [
        "Error:",
        "%Error",
        "MULTIDRIVEN",
        "BLKANDNBLK",
        "PROCASSWIRE",
        "syntax error",
    ]
    upper = log_text or ""
    return any(tok in upper for tok in fatal_tokens)

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

Use this normalized spec exactly:

{json.dumps(spec, indent=2)}

Return ONLY code.
Do not return markdown fences.
Do not return prose.

MODULE CONTRACT
- Module name must be exactly: {module_name}
- Use ONLY the ports defined in spec
- Do NOT invent extra ports
- Do NOT change port names
- Do NOT change widths
- Do NOT change directions

LANGUAGE RULES
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
  - package
  - class
- Use only simple legal constructs such as:
  - module
  - input/output/inout
  - wire
  - reg
  - assign
  - localparam
  - always @(*)
  - always @(posedge clk)
  - always @(posedge clk or negedge rst_n) when reset exists

GENERIC BEHAVIORAL MODELING RULES (MANDATORY)
- Generate a deterministic behavioral model only.
- If the spec is underspecified, choose the simplest deterministic compile-safe implementation.
- Do NOT use:
  - $random
  - $urandom
  - delays (#)
  - real / realtime / wreal
  - analog solver constructs
- Do NOT invent hidden interfaces, extra modules, or extra files.
- Keep the model self-contained and simulator-friendly.

SIGNAL OWNERSHIP RULES
- Every output must have exactly one legal driver.
- Do not assign the same signal in multiple always blocks.
- Do not assign the same signal in both combinational and sequential logic.
- Do not procedurally assign a wire-style signal.
- If a signal is assigned in an always block, declare it as reg in Verilog-2005.

RESET AND TIMING RULES
- If reset exists in the spec, implement explicit reset behavior.
- Outputs and state-holding registers must have defined reset values when reset behavior is specified.
- Pulse-style outputs must have explicit pulse width in cycles.
- If timing/latency behavior is described in the spec, implement it deterministically.

PORT COMPLETENESS RULES
- Every declared output must be driven.
- Every declared input with behavioral meaning must be consumed or intentionally reflected in logic.
- Do not leave declared outputs undriven.
- Do not leave meaningful declared inputs completely unused unless the spec explicitly allows it.

COMBINATIONAL SAFETY RULES
- Every always @(*) block must assign defaults at block entry.
- Every case statement used in combinational logic must include a default branch.
- Do not infer unintended latches.

OUTPUT REQUIREMENTS
- No placeholders
- No TODOs
- No pseudo-code
- Entire module must compile cleanly with Icarus Verilog
- The module should also be clean under Verilator lint for structural issues
- Do not add extra ports or extra files

GOOD / BAD EXAMPLES

BAD:
output [7:0] status;
always @(*) begin
  status = 8'h00;
end

GOOD:
output reg [7:0] status;
always @(*) begin
  status = 8'h00;
end

BAD:
reg done;
always @(posedge clk or negedge rst_n) done <= trigger;
always @(*) done = 1'b0;

GOOD:
reg done;
always @(posedge clk or negedge rst_n) begin
  if (!rst_n) done <= 1'b0;
  else done <= trigger;
end

BAD:
always @(*) begin
  case (sel)
    2'b00: out = a;
    2'b01: out = b;
  endcase
end

GOOD:
always @(*) begin
  out = 1'b0;
  case (sel)
    2'b00: out = a;
    2'b01: out = b;
    default: out = 1'b0;
  endcase
end

SELF-CHECK BEFORE OUTPUT
1. Module name matches exactly: {module_name}
2. Port list matches spec exactly
3. No extra ports
4. No missing ports
5. No SystemVerilog-only syntax
6. No undeclared identifiers
7. No output left undriven
8. No meaningful input left completely unused unless clearly justified by spec
9. No procedural assignment to wire-style outputs
10. No multiple drivers
11. Output is compile-clean
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
    verilator_log_filename = f"{module_name}_verilator_lint.log"
    compile_summary_filename = f"{module_name}_compile_summary.json"
    verilator_log_path = os.path.join(analog_dir, verilator_log_filename)
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
    verilator_log = ""
    repair_used = False

    try:
        cp = subprocess.run(compile_cmd, capture_output=True, text=True, check=False)
        compile_status = (cp.stdout or "") + "\n" + (cp.stderr or "")
        compile_failed = cp.returncode != 0
    except Exception as e:
        compile_status = f"Compile invocation failed: {e}"
        compile_failed = True

    fatal_lint = False
    if not compile_failed:
        try:
            vrc, verilator_log = _run_verilator_lint(model_path)
            fatal_lint = (vrc != 0) and _is_fatal_verilator_log(verilator_log)
        except Exception as e:
            verilator_log = f"Verilator invocation failed: {e}"
            fatal_lint = False

    if compile_failed or fatal_lint:
        repair_used = True
        failure_text = f"Icarus:\n{compile_status}\n\nVerilator:\n{verilator_log}"
        repair_prompt = _build_repair_prompt(prompt, spec, module_name, model, failure_text)
        repaired_raw = llm_text(repair_prompt)
        repaired_model = _extract_sv_module(repaired_raw)
        repaired_model = _force_module_name(repaired_model, module_name)

        if repaired_model and "module" in repaired_model and "endmodule" in repaired_model:
            model = repaired_model
            with open(model_path, "w", encoding="utf-8") as f:
                f.write(model.rstrip() + "\n")

            cp2 = subprocess.run(compile_cmd, capture_output=True, text=True, check=False)
            compile_status = (cp2.stdout or "") + "\n" + (cp2.stderr or "")
            compile_failed = cp2.returncode != 0

            verilator_log = ""
            fatal_lint = False
            if not compile_failed:
                try:
                    vrc2, verilator_log = _run_verilator_lint(model_path)
                    fatal_lint = (vrc2 != 0) and _is_fatal_verilator_log(verilator_log)
                except Exception as e:
                    verilator_log = f"Verilator invocation failed: {e}"
                    fatal_lint = False

    if compile_failed:
        issues.append("❌ Icarus Verilog compile failed for analog behavioral model.")



    if fatal_lint:
        issues.append("❌ Verilator lint reported fatal structural issues for analog behavioral model.")

    with open(compile_log_path, "w", encoding="utf-8") as f:
        f.write((compile_status or "").strip() + "\n")
        if issues:
            f.write("\nIssues:\n")
            for issue in issues:
                f.write(f"{issue}\n")

    with open(verilator_log_path, "w", encoding="utf-8") as f:
        f.write((verilator_log or "").strip() + "\n")

    compile_summary = {
        "block_name": block,
        "module_name": module_name,
        "model_filename": model_filename,
        "compile_cmd": compile_cmd,
        "repair_used": repair_used,
        "iverilog_failed": compile_failed,
        "verilator_fatal": fatal_lint,
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
- verilator_log: {verilator_log_filename}
- repair_used: {"yes" if repair_used else "no"}
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
    save_text_artifact_and_record(
        workflow_id,
        "Analog Behavioral Model Agent",
        "analog",
        verilator_log_filename,
        (verilator_log or "").strip()
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


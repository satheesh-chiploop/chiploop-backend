import os
import subprocess
from langchain_ollama import OllamaLLM

llm = OllamaLLM(model="mistral")

def optimizer_agent(state: dict) -> dict:
    print("\n⚡ Running Optimizer Agent...")
    rtl_code = state.get("rtl")

    if not rtl_code:
        state["status"] = "❌ No RTL available to optimize"
        return state

    prompt = f"""
You are an RTL optimizer. You are a Verilog-2005 code optimizer.
Task:
- Input RTL code:
{rtl_code}
- Optimize it for readability and synthesis.
- Ensure it remains synthesizable.
- Write synthesizable Verilog-2005 code (not SystemVerilog).
- Do not use markdown or ``` fences.
- Use lower-case signal names (clk, reset, enable) to avoid binding issues.
- Always declare every signal used in the code (clk, reset, enable, etc.).
- Do not include natural language.
- Only output Verilog starting with 'module'.
- If reset is required, use a single always block with either synchronous or asynchronous reset.
- Module name must match the spec.
- End with 'endmodule'.
"""
    optimized_rtl = llm.invoke(prompt)

    # 🔥 Strip markdown fences
    optimized_rtl = (
        optimized_rtl.replace("```verilog", "")
        .replace("```systemverilog", "")
        .replace("```", "")
        .strip()
    )
    state["optimized_rtl"] = optimized_rtl

    # Save optimized design
    optimized_file = "design_optimized.v"
    with open(optimized_file, "w", encoding="utf-8") as f:
        f.write(optimized_rtl)

    # Run lint check
    log_path = "optimizer_agent_compile.log"
    if os.path.exists(log_path):
        os.remove(log_path)

    try:
        result = subprocess.run(
            ["iverilog", "-o", "design_optimized.out", optimized_file],
            capture_output=True,
            text=True,
            check=True,
        )
        state["status"] = "✅ Optimized RTL compiled successfully"
        with open(log_path, "w", encoding="utf-8") as logf:
            logf.write(result.stdout + result.stderr)
        state["artifact"] = optimized_file
    except subprocess.CalledProcessError as e:
        state["status"] = "❌ Optimized RTL failed compilation"
        with open(log_path, "w", encoding="utf-8") as logf:
            logf.write((e.stdout or "") + (e.stderr or ""))
        state["error_log"] = e.stderr or e.stdout

    # Always attach log path
    state["artifact_log"] = log_path

    return state


import subprocess
import os

from langchain_ollama import OllamaLLM

# Initialize LLM once
llm = OllamaLLM(model="mistral")

def spec_agent(state: dict) -> dict:
    print("\n🚀 Running Spec Agent...")
    spec = state.get("spec", "")

    if not spec:
        state["status"] = "❌ No spec provided"
        return state

    # Build prompt
    prompt = f"""
     You are a Verilog-2005 code generator.

    Task:
    - Write synthesizable Verilog-2005 code (not SystemVerilog).
    - Do not use markdown or ``` fences.
    - Use lower-case signal names (clk, reset, enable) to avoid binding issues.
    - Always declare every signal used in the code (clk, reset, enable, etc.).
    - Do not include natural language.
    - Only output Verilog starting with 'module'.
    - If reset is required, use a single always block with either synchronous or asynchronous reset.
    - Module name must match the spec.
    - End with 'endmodule'.

    Spec: {spec}
    """

    rtl_code = llm.invoke(prompt)

    # 🔥 Strip markdown fences
    rtl_code = rtl_code.replace("```verilog", "").replace("```systemverilog", "").replace("```", "").strip()

    # Ensure file starts at "module"
    if "module" in rtl_code:
      rtl_code = rtl_code[rtl_code.index("module"):]

    # Call Ollama

    state["rtl"] = rtl_code

        # Save Verilog
    with open("design.v", "w", encoding="utf-8") as f:
        f.write(rtl_code)

    # Always create compile.log

    log_path = "spec_agent_compile.log"
    if os.path.exists(log_path):
        os.remove(log_path)
    with open(log_path, "w") as logf:
        logf.write("")

    # Try compilation with Icarus
    try:
        print("\n🚀 Compiling...")
        result = subprocess.run(
            ["iverilog", "-o", "design.out", "design.v"],
            check=True,
            capture_output=True,
            text=True
        )
        state["status"] = "✅ Compilation successful"
        state["compiler_output"] = result.stdout.strip()

        with open(log_path, "a") as logf:
            logf.write(result.stdout or "")
    except subprocess.CalledProcessError as e:
        state["status"] = "❌ Compilation failed"
        state["error_log"] = (e.stderr or e.stdout or "").strip()

        with open(log_path, "a") as logf:
            logf.write(state["error_log"])

    state["artifact"] = "design.v"
    state["artifact_log"] = "spec_agent_compile.log"

    return state


# Run standalone for testing
if __name__ == "__main__":
    init_state = {"spec": "4-bit counter with synchronous reset"}
    result = spec_agent(init_state)
    print(result["status"])
    if "error_log" in result:
        print(result["error_log"])


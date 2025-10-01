import subprocess
import os
import datetime

def spec_agent(state: dict) -> dict:
    print("\n🚀 Running Spec Agent (FAKE mode)...")
    spec = state.get("spec", "")

    if not spec:
        state["status"] = "❌ No spec provided"
        return state

    # Choose fake Verilog depending on spec keywords
    if "adder" in spec.lower():
        rtl_code = """\
module fake_adder (
    input wire [3:0] a,
    input wire [3:0] b,
    output wire [4:0] sum
);
    assign sum = a + b;
endmodule
"""
    elif "counter" in spec.lower():
        rtl_code = """\
module fake_counter (
    input wire clk,
    input wire reset,
    output reg [3:0] count
);
    always @(posedge clk or posedge reset) begin
        if (reset)
            count <= 4'b0000;
        else
            count <= count + 1;
    end
endmodule
"""
    else:
        rtl_code = """\
module fake_module (
    input wire clk,
    input wire reset,
    output reg flag
);
    always @(posedge clk or posedge reset) begin
        if (reset)
            flag <= 1'b0;
        else
            flag <= ~flag;
    end
endmodule
"""

    # Save Verilog file
    verilog_file = "design.v"
    with open(verilog_file, "w", encoding="utf-8") as f:
        f.write(rtl_code)

    # Always create compile.log
    log_path = "spec_agent_compile.log"
    if os.path.exists(log_path):
        os.remove(log_path)
    with open(log_path, "w") as logf:
        logf.write("")

    # Try compilation with Icarus
    try:
        print("\n🚀 Compiling fake Verilog...")
        result = subprocess.run(
            ["/usr/bin/iverilog", "-o", "design.out", verilog_file],
            check=True,
            capture_output=True,
            text=True
        )
        state["status"] = "✅ Fake compilation successful"
        state["compiler_output"] = result.stdout.strip()

        with open(log_path, "a") as logf:
            logf.write(result.stdout or "")
    except subprocess.CalledProcessError as e:
        state["status"] = "❌ Fake compilation failed"
        state["error_log"] = (e.stderr or e.stdout or "").strip()

        with open(log_path, "a") as logf:
            logf.write(state["error_log"])

    # Attach artifacts to state
    state["rtl"] = rtl_code
    state["artifact"] = verilog_file
    state["artifact_log"] = log_path

    return state


# Run standalone for testing
if __name__ == "__main__":
    init_state = {"spec": "4-bit counter with synchronous reset"}
    result = spec_agent(init_state)
    print(result["status"])
    if "error_log" in result:
        print(result["error_log"])
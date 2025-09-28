import subprocess
import os

def rtl_agent(state: dict) -> dict:
    print("\n🛠 Running RTL Agent (Lint Check)...")

    rtl_file = state.get("rtl_file", "design.v")
    if not os.path.exists(rtl_file):
        state["status"] = "❌ RTL file not found"
        return state

    log_path = "rtl_agent_compile.log"
    # Reset log at start of RTL lint
    if os.path.exists(log_path):
        os.remove(log_path)
    with open(log_path, "w") as logf:
        logf.write("")

    try:
        result = subprocess.run(
            ["iverilog", "-tnull", rtl_file],  # quick syntax check
            check=False, capture_output=True, text=True
        )
        with open(log_path, "a") as logf:  # append new results
            if result.returncode == 0:
                state["status"] = "✅ RTL passed lint check"
                state["rtl_clean"] = rtl_file
                logf.write("Lint passed successfully\n")
            else:
                state["status"] = "⚠ RTL had lint issues"
                state["lint_log"] = result.stderr or result.stdout
                logf.write(state["lint_log"] + "\n")

        state["artifact"] = rtl_file
        state["artifact_log"] = "rtl_agent_compile.log"

    except Exception as e:
        state["status"] = f"❌ RTL Agent failed: {str(e)}"

    return state

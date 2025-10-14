import os, subprocess, datetime, re
import importlib.util

def has_pyuvm():
    return importlib.util.find_spec("pyuvm") is not None


def run_agent(state: dict) -> dict:
    print("\n▶️ Running Simulation Agent...")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)

    tb_dirs = [d for d in os.listdir(workflow_dir) if d.startswith("uvm_tb_")]
    tb_dir = os.path.join(workflow_dir, tb_dirs[0]) if tb_dirs else None
    rtl_files = [f for f in os.listdir(workflow_dir) if f.endswith(".v")]
    rtl_file = os.path.join(workflow_dir, rtl_files[0]) if rtl_files else None

    if not rtl_file or not tb_dir:
        raise FileNotFoundError("Missing RTL or Testbench for simulation.")

    tb_files = [os.path.join(tb_dir, f) for f in os.listdir(tb_dir)
                if f.endswith(".sv") or f.endswith(".py")]
    sim_exe = os.path.join(workflow_dir, "sim.out")
    sim_log = os.path.join(workflow_dir, "simulation_run.log")
    sim_summary = os.path.join(workflow_dir, "simulation_summary.txt")

    try:
        # --- Detect pyuvm testbench first ---
        if any(f.endswith(".py") for f in tb_files):
            print("🐍 Detected pyuvm testbench — running Python-based simulation...")
            run_proc = subprocess.run(
                ["python3", os.path.join(tb_dir, "run_test.py")],
                capture_output=True, text=True
            )
            sim_output = run_proc.stdout + run_proc.stderr

        else:
            print("🛠 Compiling design with Verilator (with coverage)...")
            cmd_compile = [
                "verilator", "--cc", rtl_file,
                "--exe", "--build",
                "-I", tb_dir,
                *tb_files,
                "--sv", "--trace",
                "--coverage",                   # ✅ real line/toggle/branch coverage
                "-I", "/root/chiploop-backend/uvm_stub",
                "--top-module", "tb_top"
            ]

            compile_proc = subprocess.run(cmd_compile, capture_output=True, text=True)

            # --- Fallback if Verilator fails (UVM unsupported) ---
            if compile_proc.returncode != 0:
                print("⚠️ Verilator failed — retrying with Icarus Verilog (UVM-lite fallback)...")
                cmd_compile = ["iverilog", "-g2012", "-I", "/root/chiploop-backend/uvm_stub","-o", sim_exe, rtl_file, *tb_files]
                run_compile = subprocess.run(cmd_compile, capture_output=True, text=True)
                if run_compile.returncode != 0:
                    raise subprocess.CalledProcessError(
                        run_compile.returncode, cmd_compile,
                        run_compile.stdout, run_compile.stderr
                    )
                run_proc = subprocess.run(["vvp", sim_exe], capture_output=True, text=True)
                sim_output = run_proc.stdout + run_proc.stderr
            else:
                print("▶️ Running simulation...")
                run_proc = subprocess.run(["obj_dir/Vtb_top"], capture_output=True, text=True)
                sim_output = run_proc.stdout + run_proc.stderr

                # ✅ Extract real Verilator coverage if available
                vdb_path = os.path.join(workflow_dir, "sim.vdb")
                if os.path.exists(vdb_path):
                    cov_dir = os.path.join(workflow_dir, "coverage")
                    os.makedirs(cov_dir, exist_ok=True)
                    subprocess.run([
                        "verilator_coverage",
                        "--write-info", "max",
                        "--annotate", cov_dir,
                        vdb_path
                    ])
                    print(f"📊 Real Verilator coverage written to {cov_dir}")

        # --- Save logs ---
        with open(sim_log, "w", encoding="utf-8") as f:
            f.write(sim_output)

        # --- Extract test results ---
        test_results = []
        for line in sim_output.splitlines():
            if re.search(r"TEST\s*:\s*\w+", line, re.IGNORECASE):
                name_match = re.search(r"TEST\s*:\s*(\w+)", line, re.IGNORECASE)
                status_match = re.search(r"(PASS|FAIL|ERROR)", line, re.IGNORECASE)
                if name_match:
                    test_name = name_match.group(1)
                    test_status = status_match.group(1).upper() if status_match else "UNKNOWN"
                    test_results.append((test_name, test_status))

        if not test_results:
            uvm_pass = len(re.findall(r"(UVM_INFO|PYUVM_INFO)", sim_output))
            uvm_fail = len(re.findall(r"(UVM_ERROR|UVM_FATAL|PYUVM_ERROR)", sim_output))
            test_results.append(("uvm_info_blocks", f"{uvm_pass} INFO"))
            test_results.append(("uvm_error_blocks", f"{uvm_fail} ERRORS"))

        passed = len([t for t in test_results if "PASS" in t[1]])
        failed = len([t for t in test_results if "FAIL" in t[1] or "ERROR" in t[1]])
        total = len(test_results)

        # --- Build summary ---
        summary_lines = ["Simulation Summary:", "-------------------"]
        for name, status in test_results:
            emoji = "✅" if "PASS" in status else ("❌" if "FAIL" in status or "ERROR" in status else "⚪")
            summary_lines.append(f"{emoji} {name:<30} : {status}")
        summary_lines.append("-------------------")
        summary_lines.append(f"Total: {total} | Passed: {passed} | Failed: {failed}")

        summary_text = "\n".join(summary_lines)
        with open(sim_summary, "w", encoding="utf-8") as f:
            f.write(summary_text)

        print("\n" + summary_text)

        state.update({
            "status": "✅ Simulation completed",
            "artifact": sim_summary,
            "artifact_log": sim_log,
            "workflow_dir": workflow_dir,
            "workflow_id": workflow_id,
            "simulation_summary": summary_text
        })

        print(f"✅ Simulation Agent completed — results logged to {sim_summary}")
        return state

    except subprocess.CalledProcessError as e:
        print(f"❌ Simulation failed: {e}")
        with open(sim_log, "w", encoding="utf-8") as f:
            f.write(e.stdout or "")
            f.write(e.stderr or "")
        state["status"] = f"❌ Simulation failed: {e}"
        return state

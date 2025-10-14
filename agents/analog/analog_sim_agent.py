# backend/agents/analog/analog_sim_agent.py
import os, subprocess, re, json
from datetime import datetime
from typing import Dict, Any

def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Analog Sim Agent
    - Runs ngspice in batch mode on the generated netlist
    - Captures .log and .raw (if created), parses printed data into CSV
    """
    print("\n⚙️ Running Analog Sim Agent...")

    wf_id = state.get("workflow_id", "analog_default")
    wf_dir = state.get("workflow_dir", f"backend/workflows/{wf_id}")
    os.makedirs(wf_dir, exist_ok=True)

    netlist = state.get("netlist_file") or os.path.join(wf_dir, "analog_netlist.cir")
    if not os.path.exists(netlist):
        raise FileNotFoundError(f"Netlist not found: {netlist}")

    log_path = os.path.join(wf_dir, "analog_sim.log")
    raw_path = os.path.join(wf_dir, "analog_sim.raw")

    # Run ngspice
    cmd = ["ngspice", "-b", "-o", log_path, netlist]
    print(f"→ Running: {' '.join(cmd)}")
    try:
        subprocess.run(cmd, cwd=wf_dir, check=True)
    except subprocess.CalledProcessError as e:
        # still save partial logs and return failure state
        state.update({"status": f"❌ ngspice failed: {e}", "artifact_log": log_path})
        return state

    # Parse printed data from log to a CSV (simple AC/TRAN parser)
    csv_path = os.path.join(wf_dir, "analog_result.csv")
    header = None
    rows = []

    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                # match "frequency value" or "time value"
                m = re.match(r"^\s*([0-9.+eE-]+)\s+([0-9.+eE-]+)", line)
                if m:
                    if header is None:
                        header = "x,value"  # x=frequency or time depending on analysis
                    rows.append(f"{m.group(1)},{m.group(2)}")
    except Exception:
        pass

    if rows:
        with open(csv_path, "w", encoding="utf-8") as out:
            out.write(header + "\n")
            out.write("\n".join(rows))

    state.update({
        "status": "✅ Analog Simulation Completed",
        "sim_log": log_path,
        "sim_csv": csv_path if rows else None,
        "sim_raw": raw_path if os.path.exists(raw_path) else None,
        "artifact": log_path,
        "artifact_log": log_path,
        "workflow_dir": wf_dir
    })
    print(f"✅ ngspice run complete. Log: {log_path}" + (f", CSV: {csv_path}" if rows else ""))
    return state

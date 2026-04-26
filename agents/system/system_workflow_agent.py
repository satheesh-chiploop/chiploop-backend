import os
import json
import datetime
import subprocess
from agents.runtime import RUNTIME_ACTIVE_STATE_KEY, AgentContext, execute_agent
from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Workflow Agent"

def _run(context: AgentContext) -> dict:
    state = context.state
    """
    System Workflow Agent
    Validates cross-loop integration — ensures Digital, Analog, and Embedded outputs exist
    and generates a consolidated system_validation.json report.
    """

    print("\n🚀 Running System Workflow Agent...")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)

    log_path = os.path.join(workflow_dir, "system_workflow_agent.log")
    validation_path = os.path.join(workflow_dir, "system_validation.json")

    # --- Expected artifacts ---
    digital_rtl = os.path.join(workflow_dir, "rtl.v")
    analog_netlist = os.path.join(workflow_dir, "analog_netlist.cir")
    firmware_code = os.path.join(workflow_dir, "main.c")

    checks = {
        "digital": os.path.exists(digital_rtl),
        "analog": os.path.exists(analog_netlist),
        "embedded": os.path.exists(firmware_code)
    }

    # --- Determine status ---
    all_exist = all(checks.values())
    status = "✅ Validated" if all_exist else "⚠️ Partial"
    missing = [k for k, v in checks.items() if not v]
    summary = (
        "All subsystems are present and ready for integration."
        if all_exist else f"Missing components: {', '.join(missing)}"
    )

    report = {
        "workflow_id": workflow_id,
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "status": status,
        "checks": checks,
        "summary": summary
    }

    # --- Write validation report ---
    with open(validation_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    # --- Write log file ---
    with open(log_path, "w", encoding="utf-8") as logf:
        logf.write(f"System Workflow Agent executed at {datetime.datetime.utcnow()}\n")
        for domain, ok in checks.items():
            logf.write(f"{domain.upper()}: {'✅ Found' if ok else '❌ Missing'}\n")
        logf.write(f"Summary: {summary}\n")

    # --- Upload artifacts to Supabase ---
        # --- Upload artifacts to Supabase (new helper) ---
    try:
        agent_name = context.agent_name

        # 1) System validation JSON
        try:
            with open(validation_path, "r", encoding="utf-8") as f:
                validation_content = f.read()

            save_text_artifact_and_record(
                workflow_id=workflow_id,
                agent_name=agent_name,
                subdir="system",
                filename="system_validation.json",
                content=validation_content,
            )
        except Exception as e:
            print(f"⚠️ Failed to upload system validation report: {e}")

        # 2) System workflow log
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                log_content = f.read()

            save_text_artifact_and_record(
                workflow_id=workflow_id,
                agent_name=agent_name,
                subdir="system",
                filename="system_workflow_agent.log",
                content=log_content,
            )
        except Exception as e:
            print(f"⚠️ Failed to upload system workflow log: {e}")

        print("🧩 System artifacts uploaded to Supabase.")
    except Exception as e:
        print(f"⚠️ Artifact upload failed: {e}")


    # --- Finalize state ---
    state.update({
        "artifact": validation_path,
        "artifact_log": log_path,
        "status": status,
        "summary": summary,
    })

    print(f"✅ System Validation Completed → {validation_path}")
    return state


def run_agent(state: dict) -> dict:
    context = AgentContext.from_state(state, AGENT_NAME)
    if state.get(RUNTIME_ACTIVE_STATE_KEY):
        return _run(context)
    result = execute_agent(context, _run)
    state.update(result.to_state_update())
    return state

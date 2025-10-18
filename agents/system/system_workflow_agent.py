import os
import json
import datetime
import subprocess
from utils.artifact_utils import upload_artifact_generic, append_artifact_record

def run_agent(state: dict) -> dict:
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
    try:
        user_id = state.get("user_id", "anonymous")

        validation_storage = upload_artifact_generic(
            local_path=validation_path,
            user_id=user_id,
            workflow_id=workflow_id,
            agent_label="system_validation"
        )
        if validation_storage:
            append_artifact_record(workflow_id, "system_validation_report", validation_storage)

        log_storage = upload_artifact_generic(
            local_path=log_path,
            user_id=user_id,
            workflow_id=workflow_id,
            agent_label="system_logs"
        )
        if log_storage:
            append_artifact_record(workflow_id, "system_validation_log", log_storage)

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

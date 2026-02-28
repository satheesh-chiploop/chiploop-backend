import os
import json
from datetime import datetime

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "Digital Foundry Profile Agent"

def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)

    # Host-side defaults (your real install)
    pdk_root_host = os.getenv("CHIPLOOP_PDK_ROOT", "backend/pdk")
    pdk_root_host_abs = os.path.abspath(pdk_root_host)

    # Allow override later via UI scope_json, but keep deterministic defaults now
    pdk = (state.get("pdk") or os.getenv("PDK") or "sky130A").strip()
    foundry = (state.get("foundry") or "skywater").strip()

    # Validate basic presence
    pdk_path = os.path.join(pdk_root_host_abs, pdk)
    pdk_ok = os.path.exists(pdk_path)

    profile = {
        "foundry": foundry,
        "pdk": pdk,
        "pdk_root": pdk_root_host_abs,
        "process": "130nm" if "sky130" in pdk.lower() else "unknown",
        "stdcell": {"library_set": "default", "vt": ["svt"], "lib_format": "openlane-managed"},
        "corners": [
            {"name": "tt_25C_1v80", "category": "typical"},
            {"name": "ss_100C_1v60", "category": "slow"},
            {"name": "ff_0C_1v95", "category": "fast"},
        ],
        "timing": {
            "target_clock_mhz": 50,
            "clock_name": "clk",
            "reset_name": "reset_n",
        },
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "notes": "Auto-generated safe defaults. Override via UI scope/toggles later.",
    }

    # Write within workflow dir for local debug + determinism
    out_dir = os.path.join(workflow_dir, "digital", "foundry")
    os.makedirs(out_dir, exist_ok=True)

    profile_path = os.path.join(out_dir, "foundry_profile.json")
    log_path = os.path.join(out_dir, "foundry_profile.log")

    with open(profile_path, "w") as f:
        json.dump(profile, f, indent=2)

    log_lines = [
        f"[{profile['generated_at']}] {AGENT_NAME}",
        f"workflow_id={workflow_id}",
        f"workflow_dir={os.path.abspath(workflow_dir)}",
        f"pdk_root={pdk_root_host_abs}",
        f"pdk={pdk}",
        f"pdk_path={pdk_path}",
        f"pdk_ok={pdk_ok}",
    ]
    with open(log_path, "w") as f:
        f.write("\n".join(log_lines) + "\n")

    # Upload artifacts
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital/foundry", "foundry_profile.json", json.dumps(profile, indent=2))
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital/foundry", "foundry_profile.log", "\n".join(log_lines) + "\n")

    # State handoff
    state.setdefault("digital", {})
    state["digital"]["foundry_profile"] = "digital/foundry/foundry_profile.json"
    state["digital"]["pdk_ok"] = bool(pdk_ok)
    state["status"] = "✅ Foundry profile generated" if pdk_ok else "⚠️ Foundry profile generated but PDK not found on host"

    return state
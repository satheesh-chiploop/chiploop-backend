import os
import json
from datetime import datetime

import logging
logger = logging.getLogger("chiploop")

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "Digital Foundry Profile Agent"

def _read_json_safe(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _resolve_spec_json_path(state: dict, workflow_dir: str) -> str | None:
    cand = (
        state.get("digital_spec_json")
        or state.get("spec_json")
        or (state.get("digital") or {}).get("spec_json")
    )
    if cand and os.path.exists(cand):
        return cand

    spec_dir = os.path.join(workflow_dir, "spec")
    if os.path.isdir(spec_dir):
        files = sorted(
            [os.path.join(spec_dir, f) for f in os.listdir(spec_dir) if f.endswith("_spec.json")]
        )
        if files:
            return files[0]
    return None

def _extract_top_clock_reset(spec: dict) -> tuple[str, str, float, str]:
    top_module = (
        (((spec.get("hierarchy") or {}).get("top_module") or {}).get("name"))
        or spec.get("design_name")
        or spec.get("top_module")
        or spec.get("name")
        or "top"
    )

    clk_name = "clk"
    reset_name = "reset_n"
    target_clock_mhz = 50.0

    oc = spec.get("operating_constraints") or {}
    cds = oc.get("clock_domains") or []
    if cds and isinstance(cds[0], dict):
        clk_name = cds[0].get("name") or clk_name
        if cds[0].get("frequency_mhz"):
            try:
                target_clock_mhz = float(cds[0]["frequency_mhz"])
            except Exception:
                pass
        elif cds[0].get("period_ns"):
            try:
                p = float(cds[0]["period_ns"])
                if p > 0:
                    target_clock_mhz = 1000.0 / p
            except Exception:
                pass

    rs = oc.get("reset_signals") or []
    if rs and isinstance(rs[0], dict):
        reset_name = rs[0].get("name") or reset_name

    return top_module, clk_name, target_clock_mhz, reset_name

def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    workflow_dir = os.path.abspath(workflow_dir)
    os.makedirs(workflow_dir, exist_ok=True)

    # Host-side defaults (your real install)
    pdk_root_host = os.getenv("CHIPLOOP_PDK_ROOT_HOST", "backend/pdk")
    pdk_root_host_abs = os.path.abspath(pdk_root_host)

    # Allow override later via UI scope_json, but keep deterministic defaults now
    pdk = (state.get("pdk") or os.getenv("PDK") or "sky130A").strip()
    foundry = (state.get("foundry") or "skywater").strip()

    # Validate basic presence
    pdk_path = os.path.join(pdk_root_host_abs, pdk)
    pdk_ok = os.path.exists(pdk_path)

    logger.info(f"🏁 Running {AGENT_NAME}")
    logger.info(f"workflow_id={workflow_id}")

    spec_json_path = _resolve_spec_json_path(state, workflow_dir)
    spec = _read_json_safe(spec_json_path) if spec_json_path else {}

    top_module, clock_name, target_clock_mhz, reset_name = _extract_top_clock_reset(spec)

    logger.info(f"{AGENT_NAME}: spec_json_path={spec_json_path}")
    logger.info(
        f"{AGENT_NAME}: resolved top={top_module}, clk={clock_name}, "
        f"target_clock_mhz={target_clock_mhz}, reset={reset_name}"
    )

    profile = {
        "foundry": foundry,
        "pdk": pdk,
        "pdk_root": pdk_root_host_abs,
        "process": "130nm" if "sky130" in pdk.lower() else "unknown",
        "top_module": top_module,
        "spec_json": spec_json_path,
        "stdcell": {"library_set": "default", "vt": ["svt"], "lib_format": "openlane-managed"},
        "corners": [
            {"name": "tt_25C_1v80", "category": "typical"},
            {"name": "ss_100C_1v60", "category": "slow"},
            {"name": "ff_0C_1v95", "category": "fast"},
        ],
        "timing": {
            "target_clock_mhz": target_clock_mhz,
            "clock_name": clock_name,
            "reset_name": reset_name,
        },
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "notes": "Spec-aware foundry defaults; fallback-safe.",
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

    digital = state.setdefault("digital", {})
    digital["foundry_profile"] = "digital/foundry/foundry_profile.json"
    digital["pdk_ok"] = bool(pdk_ok)
    digital["spec_json"] = spec_json_path or digital.get("spec_json") or state.get("spec_json")
    digital["top_module"] = top_module
    digital["clock_name"] = clock_name
    digital["target_clock_mhz"] = target_clock_mhz
    digital["reset_name"] = reset_name
    state["pdk_root_host"] = pdk_root_host_abs
    state["pdk_variant"] = pdk
    state["status"] = "✅ Foundry profile generated" if pdk_ok else "⚠️ Foundry profile generated but PDK not found on host"
    logger.info(f"{AGENT_NAME}: profile_path={profile_path}")
    logger.info(f"{AGENT_NAME}: log_path={log_path}")

    return state

import json
import logging
import os
from typing import Optional

from ._embedded_common import ensure_workflow_dir, write_artifact

logger = logging.getLogger(__name__)

AGENT_NAME = "Embedded Reset Sequencing Agent"
PHASE = "reset_sequence"
OUTPUT_RS = "firmware/boot/reset_sequence.rs"
OUTPUT_MD = "firmware/boot/reset_sequence.md"
DEBUG_PATH = "firmware/boot/reset_sequence_debug.json"
SUMMARY_PATH = "firmware/boot/reset_sequence_summary.json"
MANIFEST_PATH = "firmware/firmware_manifest.json"


def _safe_load_json(path: str) -> Optional[dict]:
    try:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as exc:
        logger.warning("%s failed to load %s: %s", AGENT_NAME, path, exc)
    return None


def _load_manifest(state: dict, workflow_dir: str) -> dict:
    manifest = state.get("firmware_manifest") or (state.get("firmware") or {}).get("manifest")
    if isinstance(manifest, dict):
        return dict(manifest)

    manifest_path = state.get("firmware_manifest_path") or (state.get("firmware") or {}).get("manifest_path") or MANIFEST_PATH
    if manifest_path and not os.path.isabs(manifest_path):
        manifest_path = os.path.join(workflow_dir, manifest_path)
    loaded = _safe_load_json(manifest_path)
    return loaded if isinstance(loaded, dict) else {}


def _build_md(has_reset_cause_regs: bool, has_pll: bool) -> str:
    lines = [
        "## Reset Sources and Detection",
        "",
    ]
    if has_reset_cause_regs:
        lines.extend([
            "- Firmware-readable reset-cause registers are declared by the hardware contract.",
            "- Boot software should capture reset cause before clearing sticky status.",
        ])
    else:
        lines.extend([
            "- ASSUMPTION: No firmware-readable reset-cause registers are exposed in the current hardware contract.",
            "- Reset source may need to be inferred from integration context or external testbench control.",
        ])

    lines.extend([
        "",
        "## Boot-Time Sequencing Order",
        "",
        "1. Confirm reset has been released by the integration environment.",
        "2. Delay until reset-sensitive logic is stable.",
        "3. Apply clock configuration only if programmable clock control exists.",
        "4. Perform register-driven bring-up steps for the digital/analog subsystem.",
        "5. Enable interrupts or advanced services only after core bring-up checks pass.",
        "",
        "## Reset-Cause Handling Policy",
        "",
        "| Reset cause | Detection method | Firmware action |",
        "| --- | --- | --- |",
        f"| POR | {'reset-cause register' if has_reset_cause_regs else 'integration assumption'} | perform full initialization |",
        f"| WDT | {'reset-cause register' if has_reset_cause_regs else 'integration assumption'} | log/reset sticky state and perform safe re-init |",
        f"| SW | {'reset-cause register' if has_reset_cause_regs else 'integration assumption'} | allow controlled reconfiguration path if validated |",
        f"| Pin reset | {'reset-cause register' if has_reset_cause_regs else 'integration assumption'} | treat as external reset and re-run bring-up |",
        "",
        "## Minimum Delay Checklist",
        "",
        f"- After reset release: ASSUMPTION = wait at least one observable firmware step before functional MMIO.",
        f"- After clock/PLL enable: {'wait for lock/ready indication before use' if has_pll else 'no programmable PLL delay required by firmware'}",
        "- After enabling dependent subsystems: wait for ready/status indication before functional use.",
        "",
    ])
    return "\n".join(lines)


def _build_rs(has_reset_cause_regs: bool) -> str:
    if not has_reset_cause_regs:
        return """// ASSUMPTION: No firmware-readable reset-cause registers are exposed in the current hardware contract.

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ResetCause {
    Unknown,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ResetError {
    Unsupported,
}

#[inline]
pub fn read_reset_cause() -> ResetCause {
    ResetCause::Unknown
}

#[inline]
pub fn clear_reset_cause(_cause: ResetCause) {}

#[inline]
pub fn apply_boot_reset_policy(_cause: ResetCause) -> Result<(), ResetError> {
    Ok(())
}
"""
    return """// ASSUMPTION: Reset-cause registers exist, but concrete addresses are target-specific.

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ResetCause {
    PowerOn,
    Watchdog,
    Software,
    ExternalPin,
    Unknown,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ResetError {
    RegisterAccess,
}

pub trait ResetRegs {
    fn read_reset_cause_bits(&self) -> u32;
    fn clear_reset_cause_bits(&mut self, bits: u32);
}

#[inline]
pub fn decode_reset_cause(bits: u32) -> ResetCause {
    if bits & 0x1 != 0 {
        ResetCause::PowerOn
    } else if bits & 0x2 != 0 {
        ResetCause::Watchdog
    } else if bits & 0x4 != 0 {
        ResetCause::Software
    } else if bits & 0x8 != 0 {
        ResetCause::ExternalPin
    } else {
        ResetCause::Unknown
    }
}

#[inline]
pub fn read_reset_cause() -> ResetCause {
    ResetCause::Unknown
}

#[inline]
pub fn clear_reset_cause(_cause: ResetCause) {}

#[inline]
pub fn apply_boot_reset_policy(_cause: ResetCause) -> Result<(), ResetError> {
    Ok(())
}
"""


def run_agent(state: dict) -> dict:
    print(f"\n🚀 Running {AGENT_NAME}...")
    logger.info("Starting %s", AGENT_NAME)
    ensure_workflow_dir(state)

    workflow_dir = state.get("workflow_dir") or ""
    manifest = _load_manifest(state, workflow_dir)
    features = manifest.get("hardware_features") or {}
    has_reset_cause_regs = bool(features.get("has_reset_cause_registers"))
    has_pll = bool(features.get("has_programmable_pll"))

    out_md = _build_md(has_reset_cause_regs, has_pll)
    out_rs = _build_rs(has_reset_cause_regs)

    write_artifact(state, OUTPUT_MD, out_md, key=os.path.basename(OUTPUT_MD))
    write_artifact(state, OUTPUT_RS, out_rs, key=os.path.basename(OUTPUT_RS))
    write_artifact(
        state,
        DEBUG_PATH,
        json.dumps(
            {
                "agent": AGENT_NAME,
                "manifest_present": bool(manifest),
                "has_reset_cause_registers": has_reset_cause_regs,
                "has_programmable_pll": has_pll,
            },
            indent=2,
        ),
        key=os.path.basename(DEBUG_PATH),
    )
    write_artifact(
        state,
        SUMMARY_PATH,
        json.dumps(
            {
                "agent": AGENT_NAME,
                "phase": PHASE,
                "reset_sequence_md": OUTPUT_MD,
                "reset_sequence_rs": OUTPUT_RS,
            },
            indent=2,
        ),
        key=os.path.basename(SUMMARY_PATH),
    )

    manifest = dict(manifest or {})
    manifest["reset_sequence_md_path"] = OUTPUT_MD
    manifest["reset_sequence_rs_path"] = OUTPUT_RS
    write_artifact(state, MANIFEST_PATH, json.dumps(manifest, indent=2), key=os.path.basename(MANIFEST_PATH))

    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = [OUTPUT_MD, OUTPUT_RS]

    firmware_boot = state.setdefault("firmware_boot", {})
    firmware_boot["reset_sequence_md_path"] = OUTPUT_MD
    firmware_boot["reset_sequence_rs_path"] = OUTPUT_RS

    state["firmware_manifest"] = manifest
    state["firmware_manifest_path"] = MANIFEST_PATH
    state["status"] = f"✅ {AGENT_NAME} done"
    return state

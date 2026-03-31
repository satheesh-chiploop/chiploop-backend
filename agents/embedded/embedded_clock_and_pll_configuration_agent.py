
import json
import logging
import os
from typing import Optional

from ._embedded_common import ensure_workflow_dir, write_artifact

logger = logging.getLogger(__name__)

AGENT_NAME = "Embedded Clock And PLL Configuration Agent"
PHASE = "pll_config"
OUTPUT_PATH = "firmware/boot/pll_config.rs"
DEBUG_PATH = "firmware/boot/pll_config_debug.json"
SUMMARY_PATH = "firmware/boot/pll_config_summary.json"
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


def _no_pll_stub() -> str:
    return """// ASSUMPTION: The current hardware contract does not expose programmable PLL controls.
// ASSUMPTION: Firmware uses a fixed/default clock source provided by hardware integration.

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BootError {
    UnsupportedClockControl,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ClockSource {
    FixedDefault,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ClockConfig {
    pub final_hz: u32,
    pub clock_source: ClockSource,
    pub degraded_mode: bool,
}

#[inline]
pub fn configure_clocks() -> Result<ClockConfig, BootError> {
    Ok(ClockConfig {
        final_hz: 0,
        clock_source: ClockSource::FixedDefault,
        degraded_mode: false,
    })
}

#[inline]
pub fn try_enable_pll(_target_hz: u32) -> Result<(), BootError> {
    Err(BootError::UnsupportedClockControl)
}

#[inline]
pub fn wait_for_pll_lock(_timeout_us: u32) -> Result<(), BootError> {
    Err(BootError::UnsupportedClockControl)
}
"""


def _pll_trait_stub() -> str:
    return """// ASSUMPTION: Programmable clock/PLL control is present, but concrete register addresses are not yet provided.
// ASSUMPTION: Integrators should implement ClockRegs for target-specific register access.

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BootError {
    InvalidTargetHz,
    PllEnableFailed,
    PllLockTimeout,
    ClockSwitchFailed,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ClockSource {
    InternalOscillator,
    Pll,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ClockConfig {
    pub final_hz: u32,
    pub clock_source: ClockSource,
    pub degraded_mode: bool,
}

pub trait ClockRegs {
    fn enable_pll(&mut self, target_hz: u32) -> Result<(), BootError>;
    fn is_pll_locked(&self) -> bool;
    fn switch_to_pll(&mut self) -> Result<(), BootError>;
    fn switch_to_internal_oscillator(&mut self) -> Result<(), BootError>;
}

#[inline]
pub fn try_enable_pll_with<R: ClockRegs>(regs: &mut R, target_hz: u32) -> Result<(), BootError> {
    if target_hz == 0 {
        return Err(BootError::InvalidTargetHz);
    }
    regs.enable_pll(target_hz)
}

#[inline]
pub fn wait_for_pll_lock_with<R: ClockRegs>(regs: &R, timeout_us: u32) -> Result<(), BootError> {
    let mut elapsed = 0u32;
    while elapsed < timeout_us {
        if regs.is_pll_locked() {
            return Ok(());
        }
        elapsed += 1;
    }
    Err(BootError::PllLockTimeout)
}

#[inline]
pub fn configure_clocks_with<R: ClockRegs>(regs: &mut R, target_hz: u32) -> Result<ClockConfig, BootError> {
    match try_enable_pll_with(regs, target_hz).and_then(|_| wait_for_pll_lock_with(regs, 1000)) {
        Ok(()) => {
            regs.switch_to_pll()?;
            Ok(ClockConfig {
                final_hz: target_hz,
                clock_source: ClockSource::Pll,
                degraded_mode: false,
            })
        }
        Err(_) => {
            regs.switch_to_internal_oscillator()?;
            Ok(ClockConfig {
                final_hz: 0,
                clock_source: ClockSource::InternalOscillator,
                degraded_mode: true,
            })
        }
    }
}

// Generic wrappers retained for a stable API surface.
#[inline]
pub fn configure_clocks() -> Result<ClockConfig, BootError> {
    Ok(ClockConfig {
        final_hz: 0,
        clock_source: ClockSource::InternalOscillator,
        degraded_mode: true,
    })
}

#[inline]
pub fn try_enable_pll(_target_hz: u32) -> Result<(), BootError> {
    Err(BootError::PllEnableFailed)
}

#[inline]
pub fn wait_for_pll_lock(_timeout_us: u32) -> Result<(), BootError> {
    Err(BootError::PllLockTimeout)
}
"""


def run_agent(state: dict) -> dict:
    print(f"\n🚀 Running {AGENT_NAME}...")
    logger.info("Starting %s", AGENT_NAME)
    ensure_workflow_dir(state)

    workflow_dir = state.get("workflow_dir") or ""
    manifest = _load_manifest(state, workflow_dir)
    has_pll = bool(((manifest.get("hardware_features") or {}).get("has_programmable_pll")))

    out = _pll_trait_stub() if has_pll else _no_pll_stub()

    write_artifact(state, OUTPUT_PATH, out, key=os.path.basename(OUTPUT_PATH))
    write_artifact(
        state,
        DEBUG_PATH,
        json.dumps(
            {
                "agent": AGENT_NAME,
                "manifest_present": bool(manifest),
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
                "pll_config_path": OUTPUT_PATH,
                "has_programmable_pll": has_pll,
            },
            indent=2,
        ),
        key=os.path.basename(SUMMARY_PATH),
    )

    manifest = dict(manifest or {})
    manifest["pll_config_path"] = OUTPUT_PATH
    manifest["hardware_features"] = dict(manifest.get("hardware_features") or {})
    write_artifact(state, MANIFEST_PATH, json.dumps(manifest, indent=2), key=os.path.basename(MANIFEST_PATH))

    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH

    firmware_boot = state.setdefault("firmware_boot", {})
    firmware_boot["pll_config_path"] = OUTPUT_PATH

    state["firmware_manifest"] = manifest
    state["firmware_manifest_path"] = MANIFEST_PATH
    state["status"] = f"✅ {AGENT_NAME} done"
    return state



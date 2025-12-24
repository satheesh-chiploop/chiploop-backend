import os
import json
from datetime import datetime
from typing import Any, Dict, Optional, List

from utils.artifact_utils import save_text_artifact_and_record


def _log(log_path: str, msg: str) -> None:
    print(msg)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now().isoformat()}] {msg}\n")


def _find_existing_json_path(state: dict, candidates: List[str]) -> Optional[str]:
    for k in candidates:
        v = state.get(k)
        if isinstance(v, str) and v and os.path.exists(v) and v.lower().endswith(".json"):
            return v
    return None


def _load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _gen_bridge_readme(spec: Dict[str, Any]) -> str:
    base = (spec.get("mmio") or {}).get("base_address", "0x40000000")
    return f"""\
# System ISS Bridge (Scaffolding)

Goal: run firmware on an ISS and connect its MMIO + IRQ traffic into RTL.

**Realistic SoC-style co-sim:**
- Firmware compiled to ELF
- ISS executes firmware
- ISS MMIO reads/writes -> bridged into RTL AXI4-Lite regs
- IRQ lines in RTL -> delivered to ISS interrupt controller

This agent generates scaffolding (not a full product-grade bridge) because:
- The exact ISS choice varies (QEMU / Spike / Fast Models / Renode)
- The simulator varies (Verilator / VCS / Questa / Xcelium)

## Recommended MVP path (fastest)
Use **Verilator + a C++ test harness** that:
1) Loads firmware ELF into a tiny CPU model OR uses an external ISS stub
2) Intercepts MMIO at base address {base}
3) Converts reads/writes into AXI-Lite transactions against RTL

## Generated files
- iss_bridge_api.md : interface contract for MMIO/IRQ
- verilator_harness_skeleton.cpp : shows where to hook MMIO -> AXI-Lite
- run_notes.md : step-by-step manual run instructions (template)
"""


def _gen_api_md(spec: Dict[str, Any]) -> str:
    mmio = spec.get("mmio") or {}
    base = mmio.get("base_address", "0x40000000")
    return f"""\
# ISS Bridge API Contract (Template)

## MMIO region
Base address: {base}

Expected operations:
- read32(addr)  -> returns 32-bit data from RTL register map
- write32(addr, data, strobe) -> updates RTL regs based on byte strobes

## IRQ delivery
- RTL asserts irq[n]
- Bridge delivers interrupt event into ISS (implementation-specific)

## AXI-Lite mapping
Bridge must translate MMIO accesses into AXI-Lite:
- write: AW + W -> B
- read : AR -> R

This contract is intentionally minimal to keep it tool-agnostic.
"""


def _gen_verilator_harness_cpp() -> str:
    return r"""\
/*
 * Verilator harness skeleton for ISS<->RTL MMIO bridge.
 * This is scaffolding: you must plug in your chosen ISS.
 *
 * Typical approach:
 * - Choose an ISS (QEMU/Spike/custom) OR a stub that emits MMIO callbacks
 * - For each MMIO access in the MMIO window:
 *     -> perform AXI-Lite transaction against RTL model
 */

#include <cstdint>
#include <cstdio>

static uint32_t axi_lite_read32(uint32_t addr) {
  // TODO: drive ARVALID/ARADDR, wait for ARREADY, then wait for RVALID, capture RDATA
  // return rdata;
  return 0;
}

static void axi_lite_write32(uint32_t addr, uint32_t data, uint8_t wstrb) {
  // TODO: drive AWVALID/AWADDR + WVALID/WDATA/WSTRB, wait for ready, then wait for BVALID
}

int main(int argc, char** argv) {
  // TODO: init verilated model, clock, reset

  // TODO: init ISS + register MMIO callbacks.
  // Example callback signatures:
  //   uint32_t mmio_read(uint32_t addr);
  //   void mmio_write(uint32_t addr, uint32_t data, uint8_t wstrb);

  // Main loop: step ISS and step RTL together
  while (true) {
    // 1) step ISS some cycles/instructions
    // 2) handle MMIO events -> axi_lite_* calls
    // 3) toggle clock and eval RTL
  }

  return 0;
}
"""


def _gen_run_notes() -> str:
    return """\
# Run Notes (Template)

Pick your stack:

## Stack A: Verilator + custom harness (fast)
1) verilator -cc <rtl>.v axi_lite_regs.sv --exe verilator_harness_skeleton.cpp
2) make -C obj_dir -f V<top>.mk
3) run obj_dir/V<top> <firmware.elf>

## Stack B: Commercial sim + DPI + ISS
- Use VCS/Questa/Xcelium
- Use DPI-C to implement MMIO callbacks
- Connect to ISS via socket or in-process library

## Stack C: QEMU device model
- Implement an emulated device region in QEMU at the MMIO base
- That device forwards transactions to RTL via sockets/shared memory
"""


def run_agent(state: dict) -> dict:
    print("\n🧠 Running System ISS Bridge Agent (scaffolding)…")

    workflow_id = state.get("workflow_id", "default")
    os.makedirs("artifact", exist_ok=True)
    log_path = os.path.join("artifact", "system_iss_bridge_agent.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("System ISS Bridge Agent Log\n")

    embedded_spec_path = _find_existing_json_path(
        state,
        candidates=[
            "embedded_spec_json_path",
            "embedded_spec_path",
            "code_spec_json",
            "spec_json_path",
        ],
    )

    embedded_spec = None
    if embedded_spec_path:
        embedded_spec = _load_json(embedded_spec_path)
        _log(log_path, f"Loaded embedded spec: {embedded_spec_path}")
    elif isinstance(state.get("embedded_spec_json"), dict):
        embedded_spec = state["embedded_spec_json"]
        _log(log_path, "Loaded embedded spec from state['embedded_spec_json']")
    else:
        _log(log_path, "ERROR: Missing embedded spec for ISS bridge.")
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name="System ISS Bridge Agent",
            subdir="system",
            filename="system_iss_bridge_agent.log",
            content=open(log_path, "r", encoding="utf-8").read(),
        )
        state["system_iss_bridge_error"] = "missing_embedded_spec"
        return state

    readme = _gen_bridge_readme(embedded_spec)
    api = _gen_api_md(embedded_spec)
    harness = _gen_verilator_harness_cpp()
    run_notes = _gen_run_notes()

    save_text_artifact_and_record(workflow_id, "System ISS Bridge Agent", "system/iss_bridge", "README.md", readme)
    save_text_artifact_and_record(workflow_id, "System ISS Bridge Agent", "system/iss_bridge", "iss_bridge_api.md", api)
    save_text_artifact_and_record(workflow_id, "System ISS Bridge Agent", "system/iss_bridge", "verilator_harness_skeleton.cpp", harness)
    save_text_artifact_and_record(workflow_id, "System ISS Bridge Agent", "system/iss_bridge", "run_notes.md", run_notes)

    save_text_artifact_and_record(
        workflow_id=workflow_id,
        agent_name="System ISS Bridge Agent",
        subdir="system",
        filename="system_iss_bridge_agent.log",
        content=open(log_path, "r", encoding="utf-8").read(),
    )

    state["system_iss_bridge_outputs"] = [
        "backend/workflows/.../system/iss_bridge/README.md",
        "backend/workflows/.../system/iss_bridge/iss_bridge_api.md",
        "backend/workflows/.../system/iss_bridge/verilator_harness_skeleton.cpp",
        "backend/workflows/.../system/iss_bridge/run_notes.md",
    ]
    return state

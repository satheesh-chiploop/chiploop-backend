
import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from utils.artifact_utils import save_text_artifact_and_record

logger = logging.getLogger(__name__)

AGENT_NAME = "System CoSim Integration Agent"


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


def _safe_name(name: str, default: str) -> str:
    import re
    text = (name or default).strip()
    text = re.sub(r"[^A-Za-z0-9_]", "_", text)
    if not text:
        return default
    if text[0].isdigit():
        text = f"n_{text}"
    return text


def _extract_mmio_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    regmap = spec.get("mmio") or spec.get("register_map") or {}
    base = regmap.get("base_address") or spec.get("base_address") or "0x00000000"
    regs = regmap.get("registers") or spec.get("registers") or []
    regs = [r for r in regs if isinstance(r, dict)]
    regs = sorted(regs, key=lambda r: int(str(r.get("offset", "0")), 16) if str(r.get("offset", "0")).lower().startswith("0x") else int(r.get("offset", 0)))
    return {"base_address": base, "registers": regs}


def _gen_bus_adapter_sv(mmio: Dict[str, Any]) -> str:
    regs = mmio.get("registers") or []
    reg_defs = []
    read_cases = []
    write_cases = []
    for idx, reg in enumerate(regs):
        name = _safe_name(str(reg.get("name") or f"reg_{idx}").lower(), f"reg_{idx}")
        offset = reg.get("offset") or f"0x{idx*4:08X}"
        access = str(reg.get("access") or "RW").upper()
        reg_defs.append(f"  output reg [31:0] {name},")
        if access in {"RO"}:
            read_cases.append(f"      32'h{int(str(offset), 16) if str(offset).lower().startswith('0x') else int(offset):08X}: prdata = {name};")
        else:
            read_cases.append(f"      32'h{int(str(offset), 16) if str(offset).lower().startswith('0x') else int(offset):08X}: prdata = {name};")
            write_cases.append(f"""      32'h{int(str(offset), 16) if str(offset).lower().startswith('0x') else int(offset):08X}: begin
        if (pwrite && psel && penable) {name} <= pwdata;
      end""")
    if reg_defs:
        reg_defs[-1] = reg_defs[-1].rstrip(',')
    else:
        reg_defs.append("  // No explicit registers found in spec.")
    read_case_text = "\n".join(read_cases) if read_cases else "      default: prdata = 32'h0;"
    write_case_text = "\n".join(write_cases) if write_cases else "      default: begin end"
    return f"""// Auto-generated simple APB/MMIO seam.
// Generated from embedded spec/register map. No protocol-specific hardcoding beyond
// the selected generic APB-style seam used for co-simulation scaffolding.

module cosim_mmio_seam (
  input  wire        pclk,
  input  wire        presetn,
  input  wire [31:0] paddr,
  input  wire [31:0] pwdata,
  input  wire        pwrite,
  input  wire        psel,
  input  wire        penable,
  output reg  [31:0] prdata,
  output wire        pready,
{chr(10).join(reg_defs)}
);

  assign pready = 1'b1;

  always @(posedge pclk) begin
    if (!presetn) begin
{chr(10).join([f"      { _safe_name(str(r.get('name') or f'reg_{i}').lower(), f'reg_{i}') } <= 32'b0;" for i, r in enumerate(regs)]) if regs else "      prdata <= 32'b0;"}
    end else begin
      case (paddr)
{write_case_text}
        default: begin end
      endcase
    end
  end

  always @(*) begin
    prdata = 32'h0;
    case (paddr)
{read_case_text}
      default: prdata = 32'h0;
    endcase
  end

endmodule
"""


def _gen_firmware_header(mmio: Dict[str, Any]) -> str:
    base = mmio.get("base_address", "0x00000000")
    lines = [
        "#pragma once",
        "#include <cstdint>",
        "",
        "// Auto-generated MMIO header for co-simulation access.",
        "namespace cosim_mmio {",
        f"  static constexpr uintptr_t BASE = {base};",
        "",
        "  inline void write32(uintptr_t off, uint32_t v) {",
        "    *reinterpret_cast<volatile uint32_t*>(BASE + off) = v;",
        "  }",
        "",
        "  inline uint32_t read32(uintptr_t off) {",
        "    return *reinterpret_cast<volatile uint32_t*>(BASE + off);",
        "  }",
        "",
    ]
    for idx, reg in enumerate(mmio.get("registers") or []):
        name = _safe_name(str(reg.get("name") or f"REG_{idx}").upper(), f"REG_{idx}")
        offset = reg.get("offset") or f"0x{idx*4:08X}"
        lines.append(f"  static constexpr uintptr_t {name}_OFF = {offset};")
    lines.extend(["}", ""])
    return "\n".join(lines)


def _gen_cocotb_smoke(mmio: Dict[str, Any]) -> str:
    first_offset = "0x0"
    regs = mmio.get("registers") or []
    if regs:
        first_offset = str(regs[0].get("offset") or "0x0")
    return f"""# Auto-generated cocotb smoke template.
import cocotb
from cocotb.triggers import RisingEdge, Timer

@cocotb.test()
async def smoke_cosim_seam(dut):
    dut.presetn.value = 0
    for _ in range(5):
        await RisingEdge(dut.pclk)
    dut.presetn.value = 1
    for _ in range(5):
        await RisingEdge(dut.pclk)

    # Placeholder: integrate a bus functional model/driver here.
    # Example first register offset from spec: {first_offset}
    await Timer(1, units="us")
"""


def _gen_manifest(mmio: Dict[str, Any], spec_path: Optional[str]) -> str:
    payload = {
        "agent": AGENT_NAME,
        "source_spec_path": spec_path,
        "base_address": mmio.get("base_address"),
        "register_count": len(mmio.get("registers") or []),
        "registers": [
            {
                "name": reg.get("name"),
                "offset": reg.get("offset"),
                "access": reg.get("access"),
            }
            for reg in (mmio.get("registers") or [])
        ],
    }
    return json.dumps(payload, indent=2)


def _gen_readme(mmio: Dict[str, Any]) -> str:
    return f"""# System Co-Simulation Integration

This folder contains deterministic co-simulation scaffolding generated from the embedded spec.

Base address: {mmio.get("base_address")}
Register count: {len(mmio.get("registers") or [])}

Artifacts:
- cosim_mmio_seam.sv : generic APB-style MMIO seam for co-simulation scaffolding
- ip_mmio.h          : firmware-side MMIO header
- cocotb_smoke_test.py : smoke test template
- integration_manifest.json : summary of the generated integration view

Notes:
- This agent does not invent DMA/IRQ/PLL semantics.
- It emits a generic MMIO seam and firmware access layer from the register map only.
- Protocol-specific or SoC-specific integration can build on top of this scaffold.
"""


def run_agent(state: dict) -> dict:
    print("\n🧬 Running System CoSim Integration Agent…")

    workflow_id = state.get("workflow_id", "default")
    os.makedirs("artifact", exist_ok=True)
    log_path = os.path.join("artifact", "system_cosim_integration_agent.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("System CoSim Integration Agent Log\n")

    spec_path = _find_existing_json_path(
        state,
        candidates=[
            "embedded_spec_json_path",
            "embedded_spec_path",
            "code_spec_json",
            "spec_json_path",
            "firmware_register_map_path",
        ],
    )

    spec = None
    if spec_path:
        spec = _load_json(spec_path)
        _log(log_path, f"Loaded spec: {spec_path}")
    elif isinstance(state.get("embedded_spec_json"), dict):
        spec = state["embedded_spec_json"]
        _log(log_path, "Loaded embedded spec from state['embedded_spec_json']")
    elif isinstance(state.get("firmware_register_map"), dict):
        spec = {"register_map": state["firmware_register_map"]}
        _log(log_path, "Loaded register map from state['firmware_register_map']")
    else:
        _log(log_path, "ERROR: Missing embedded spec/register map for cosim integration.")
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=AGENT_NAME,
            subdir="system",
            filename="system_cosim_integration_agent.log",
            content=open(log_path, "r", encoding="utf-8").read(),
        )
        state["system_cosim_error"] = "missing_embedded_spec"
        return state

    mmio = _extract_mmio_spec(spec)
    sv = _gen_bus_adapter_sv(mmio)
    hdr = _gen_firmware_header(mmio)
    tb = _gen_cocotb_smoke(mmio)
    manifest = _gen_manifest(mmio, spec_path)
    readme = _gen_readme(mmio)

    save_text_artifact_and_record(workflow_id, AGENT_NAME, "system/cosim", "cosim_mmio_seam.sv", sv)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "system/cosim", "ip_mmio.h", hdr)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "system/cosim", "cocotb_smoke_test.py", tb)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "system/cosim", "integration_manifest.json", manifest)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "system/cosim", "README.md", readme)

    save_text_artifact_and_record(
        workflow_id=workflow_id,
        agent_name=AGENT_NAME,
        subdir="system",
        filename="system_cosim_integration_agent.log",
        content=open(log_path, "r", encoding="utf-8").read(),
    )

    state["system_cosim_outputs"] = [
        f"backend/workflows/{workflow_id}/system/cosim/cosim_mmio_seam.sv",
        f"backend/workflows/{workflow_id}/system/cosim/ip_mmio.h",
        f"backend/workflows/{workflow_id}/system/cosim/cocotb_smoke_test.py",
        f"backend/workflows/{workflow_id}/system/cosim/integration_manifest.json",
        f"backend/workflows/{workflow_id}/system/cosim/README.md",
    ]
    state["system_cosim_manifest"] = f"backend/workflows/{workflow_id}/system/cosim/integration_manifest.json"
    return state

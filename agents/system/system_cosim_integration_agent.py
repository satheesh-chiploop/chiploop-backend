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


def _gen_axi_lite_slave_sv(spec: Dict[str, Any]) -> str:
    mmio = spec.get("mmio", {})
    regs = mmio.get("registers", [])
    # Minimal 2-register map for CONTROL0/STATUS0 (expand later)
    return r"""
// Auto-generated AXI4-Lite register block (minimal scaffolding).
// NOTE: This is a template. In a real design you'd integrate with your RTL signals.

module axi_lite_regs #(
  parameter ADDR_WIDTH = 32,
  parameter DATA_WIDTH = 32
)(
  input  wire                   aclk,
  input  wire                   aresetn,

  // AXI4-Lite slave interface
  input  wire [ADDR_WIDTH-1:0]  s_axi_awaddr,
  input  wire                   s_axi_awvalid,
  output reg                    s_axi_awready,

  input  wire [DATA_WIDTH-1:0]  s_axi_wdata,
  input  wire [(DATA_WIDTH/8)-1:0] s_axi_wstrb,
  input  wire                   s_axi_wvalid,
  output reg                    s_axi_wready,

  output reg  [1:0]             s_axi_bresp,
  output reg                    s_axi_bvalid,
  input  wire                   s_axi_bready,

  input  wire [ADDR_WIDTH-1:0]  s_axi_araddr,
  input  wire                   s_axi_arvalid,
  output reg                    s_axi_arready,

  output reg  [DATA_WIDTH-1:0]  s_axi_rdata,
  output reg  [1:0]             s_axi_rresp,
  output reg                    s_axi_rvalid,
  input  wire                   s_axi_rready,

  // Register outputs to connect into RTL
  output reg  [31:0]            reg_control0,
  input  wire [31:0]            reg_status0
);

  // Address decode (byte addressing)
  localparam CTRL0_OFF  = 32'h0000_0000;
  localparam STAT0_OFF  = 32'h0000_0004;
  localparam IRQST_OFF  = 32'h0000_0008;
  localparam IRQMSK_OFF = 32'h0000_000C;

  // Simple single-beat handshake implementation
  always @(posedge aclk) begin
    if (!aresetn) begin
      s_axi_awready <= 1'b0;
      s_axi_wready  <= 1'b0;
      s_axi_bvalid  <= 1'b0;
      s_axi_bresp   <= 2'b00;

      s_axi_arready <= 1'b0;
      s_axi_rvalid  <= 1'b0;
      s_axi_rresp   <= 2'b00;
      s_axi_rdata   <= 32'b0;

      reg_control0  <= 32'b0;
    end else begin
      // Defaults
      s_axi_awready <= 1'b1;
      s_axi_wready  <= 1'b1;
      s_axi_arready <= 1'b1;

      // Write channel
      if (s_axi_awvalid && s_axi_wvalid && s_axi_awready && s_axi_wready) begin
        case (s_axi_awaddr[7:0])
          CTRL0_OFF[7:0]: begin
            // Apply byte strobes
            if (s_axi_wstrb[0]) reg_control0[7:0]   <= s_axi_wdata[7:0];
            if (s_axi_wstrb[1]) reg_control0[15:8]  <= s_axi_wdata[15:8];
            if (s_axi_wstrb[2]) reg_control0[23:16] <= s_axi_wdata[23:16];
            if (s_axi_wstrb[3]) reg_control0[31:24] <= s_axi_wdata[31:24];
          end
          default: begin
            // no-op
          end
        endcase

        s_axi_bvalid <= 1'b1;
        s_axi_bresp  <= 2'b00; // OKAY
      end

      if (s_axi_bvalid && s_axi_bready) begin
        s_axi_bvalid <= 1'b0;
      end

      // Read channel
      if (s_axi_arvalid && s_axi_arready) begin
        case (s_axi_araddr[7:0])
          CTRL0_OFF[7:0]: s_axi_rdata <= reg_control0;
          STAT0_OFF[7:0]: s_axi_rdata <= reg_status0;
          default:        s_axi_rdata <= 32'hDEAD_BEEF;
        endcase
        s_axi_rvalid <= 1'b1;
        s_axi_rresp  <= 2'b00; // OKAY
      end

      if (s_axi_rvalid && s_axi_rready) begin
        s_axi_rvalid <= 1'b0;
      end
    end
  end

endmodule
"""


def _gen_firmware_header(spec: Dict[str, Any]) -> str:
    mmio = spec.get("mmio", {})
    base = mmio.get("base_address", "0x40000000")
    regs = mmio.get("registers", [])
    # Emit offsets and simple helpers (C/C++)
    return f"""\
#pragma once
#include <cstdint>

// Auto-generated MMIO header for firmware/ISS access.

namespace ip_mmio {{
  static constexpr uintptr_t BASE = {base};

  static constexpr uintptr_t CONTROL0_OFF = 0x00;
  static constexpr uintptr_t STATUS0_OFF  = 0x04;
  static constexpr uintptr_t IRQ_STATUS_OFF = 0x08;
  static constexpr uintptr_t IRQ_MASK_OFF   = 0x0C;

  inline void write32(uintptr_t off, uint32_t v) {{
    *reinterpret_cast<volatile uint32_t*>(BASE + off) = v;
  }}

  inline uint32_t read32(uintptr_t off) {{
    return *reinterpret_cast<volatile uint32_t*>(BASE + off);
  }}
}}
"""


def _gen_cocotb_smoke() -> str:
    return r"""\
# Minimal cocotb smoke test for AXI-Lite regs (template).
import cocotb
from cocotb.triggers import RisingEdge, Timer

@cocotb.test()
async def smoke_axi_regs(dut):
    # Reset
    dut.aresetn.value = 0
    for _ in range(5):
        await RisingEdge(dut.aclk)
    dut.aresetn.value = 1
    for _ in range(5):
        await RisingEdge(dut.aclk)

    # This is a placeholder. In practice you use a cocotb AXI-Lite driver.
    # Here we just demonstrate clocking and existence.
    await Timer(1, units="us")
"""


def _gen_readme() -> str:
    return """\
# System Co-Simulation Integration (Scaffolding)

This folder contains auto-generated scaffolding to integrate:
- RTL (Verilog/SystemVerilog) exposing an AXI4-Lite register block
- Firmware (C/C++) using MMIO reads/writes
- A cosim harness entry point (placeholder)

Industry patterns:
1) RTL-only sim (UVM/cocotb) for IP correctness
2) Firmware-on-ISS + RTL regs via AXI-Lite for realistic HW/SW bring-up (SoC-style co-sim)

Generated artifacts:
- axi_lite_regs.sv   : Minimal AXI-Lite register block template (CONTROL0/STATUS0)
- ip_mmio.h          : Firmware header for MMIO register access
- cocotb_smoke_test.py: Minimal cocotb smoke test template
"""


def run_agent(state: dict) -> dict:
    print("\n🧬 Running System CoSim Integration Agent (AXI-Lite seam)…")

    workflow_id = state.get("workflow_id", "default")
    os.makedirs("artifact", exist_ok=True)
    log_path = os.path.join("artifact", "system_cosim_integration_agent.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("System CoSim Integration Agent Log\n")

    # Use embedded spec as the single source of truth for MMIO map
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
        _log(log_path, "ERROR: Missing embedded spec for cosim integration.")
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name="System CoSim Integration Agent",
            subdir="system",
            filename="system_cosim_integration_agent.log",
            content=open(log_path, "r", encoding="utf-8").read(),
        )
        state["system_cosim_error"] = "missing_embedded_spec"
        return state

    # Generate artifacts
    sv = _gen_axi_lite_slave_sv(embedded_spec)
    hdr = _gen_firmware_header(embedded_spec)
    tb = _gen_cocotb_smoke()
    readme = _gen_readme()

    save_text_artifact_and_record(workflow_id, "System CoSim Integration Agent", "system/cosim", "axi_lite_regs.sv", sv)
    save_text_artifact_and_record(workflow_id, "System CoSim Integration Agent", "system/cosim", "ip_mmio.h", hdr)
    save_text_artifact_and_record(workflow_id, "System CoSim Integration Agent", "system/cosim", "cocotb_smoke_test.py", tb)
    save_text_artifact_and_record(workflow_id, "System CoSim Integration Agent", "system/cosim", "README.md", readme)

    save_text_artifact_and_record(
        workflow_id=workflow_id,
        agent_name="System CoSim Integration Agent",
        subdir="system",
        filename="system_cosim_integration_agent.log",
        content=open(log_path, "r", encoding="utf-8").read(),
    )

    state["system_cosim_outputs"] = [
        "backend/workflows/.../system/cosim/axi_lite_regs.sv",
        "backend/workflows/.../system/cosim/ip_mmio.h",
        "backend/workflows/.../system/cosim/cocotb_smoke_test.py",
        "backend/workflows/.../system/cosim/README.md",
    ]
    return state

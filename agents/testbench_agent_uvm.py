import os
import re
import json
import logging
import datetime
import requests
from pathlib import Path
from portkey_ai import Portkey
from openai import OpenAI

logger = logging.getLogger(__name__)

# --- Configuration ---
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
USE_LOCAL_OLLAMA = os.getenv("USE_LOCAL_OLLAMA", "false").lower() == "true"
PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")
client_portkey = Portkey(api_key=PORTKEY_API_KEY)
client_openai = OpenAI()


def testbench_agent_uvm(state: dict) -> dict:
    """
    Generate a complete UVM-style verification scaffold for the given RTL and spec.
    Supports multi-user isolation, multi-clock/reset, and LLM-based stimulus generation.
    """
    try:
        print("\n🧪 Running UVM Testbench Agent...")

        # --- Multi-user workflow setup ---
        workflow_id = state.get("workflow_id", "default")
        workflow_dir = Path(state.get("workflow_dir", f"backend/workflows/{workflow_id}"))
        workflow_dir.mkdir(parents=True, exist_ok=True)
        os.chdir(workflow_dir)

        rtl_path = Path(state.get("artifact", workflow_dir / "auto_module.v"))
        spec_path = Path(state.get("spec_json", workflow_dir / "auto_module_spec.json"))
        log_path = workflow_dir / "testbench_agent_uvm_compile.log"

        if not rtl_path.exists():
            raise FileNotFoundError(f"RTL file not found: {rtl_path}")
        if not spec_path.exists():
            raise FileNotFoundError(f"Spec file not found: {spec_path}")

        rtl_text = rtl_path.read_text()
        spec_json = json.loads(spec_path.read_text())

        # --- Extract context ---
        module_match = re.search(r"module\s+(\w+)\s*\((.*?)\);", rtl_text, re.S)
        dut_name = module_match.group(1) if module_match else "dut"
        ports_raw = module_match.group(2) if module_match else ""
        ports = [p.strip() for p in re.split(r"[,\\n]", ports_raw) if p.strip()]

        clocks = spec_json.get("clock", [{"name": "clk"}])
        resets = spec_json.get("reset", [{"name": "reset"}])
        domain_info = "\n".join([
            f"- Clock {i+1}: {clk.get('name','clk')} @ {clk.get('frequency_mhz',100)}MHz "
            f"(duty={clk.get('duty_cycle',0.5)}) | Reset: {resets[min(i,len(resets)-1)].get('name','reset')}"
            for i, clk in enumerate(clocks)
        ])

        tb_dir = workflow_dir / f"uvm_tb_{dut_name}"
        tb_dir.mkdir(parents=True, exist_ok=True)

        # --- Base UVM components (unchanged logic) ---
        env = f"""\
`ifndef {dut_name.upper()}_ENV_SV
`define {dut_name.upper()}_ENV_SV
class {dut_name}_env extends uvm_env;
  `uvm_component_utils({dut_name}_env)
  {dut_name}_drv drv;
  {dut_name}_mon mon;
  {dut_name}_seq seq;
  function new(string name, uvm_component parent); super.new(name,parent); endfunction
  function void build_phase(uvm_phase phase);
    super.build_phase(phase);
    drv={dut_name}_drv::type_id::create("drv",this);
    mon={dut_name}_mon::type_id::create("mon",this);
    seq={dut_name}_seq::type_id::create("seq",this);
  endfunction
endclass
`endif
"""
        drv = f"""\
`ifndef {dut_name.upper()}_DRV_SV
`define {dut_name.upper()}_DRV_SV
class {dut_name}_drv extends uvm_driver #(uvm_sequence_item);
  `uvm_component_utils({dut_name}_drv)
  virtual {dut_name}_if vif;
  function new(string name, uvm_component parent); super.new(name,parent); endfunction
  task run_phase(uvm_phase phase); `uvm_info("DRV","Running driver phase",UVM_LOW) endtask
endclass
`endif
"""
        mon = f"""\
`ifndef {dut_name.upper()}_MON_SV
`define {dut_name.upper()}_MON_SV
class {dut_name}_mon extends uvm_monitor;
  `uvm_component_utils({dut_name}_mon)
  virtual {dut_name}_if vif;
  function new(string name, uvm_component parent); super.new(name,parent); endfunction
  task run_phase(uvm_phase phase); `uvm_info("MON","Running monitor phase",UVM_LOW) endtask
endclass
`endif
"""
        seq = f"""\
`ifndef {dut_name.upper()}_SEQ_SV
`define {dut_name.upper()}_SEQ_SV
class {dut_name}_seq extends uvm_sequence #(uvm_sequence_item);
  `uvm_object_utils({dut_name}_seq)
  function new(string name="{dut_name}_seq"); super.new(name); endfunction
  task body(); `uvm_info("SEQ","Running sequence",UVM_LOW) endtask
endclass
`endif
"""
        iface_ports = "\n  ".join([f"logic {p.split()[-1].strip()};" for p in ports if p])
        iface = f"""\
interface {dut_name}_if(input logic {clocks[0].get('name','clk')});
  // Auto-generated interface
  {iface_ports}
endinterface
"""
        tb_top = f"""\
`timescale 1ns/1ps
`include "uvm_macros.svh"
import uvm_pkg::*;
module tb_{dut_name};
  bit {clocks[0].get('name','clk')};
  always #5 {clocks[0].get('name','clk')} = ~{clocks[0].get('name','clk')};
  {dut_name}_if tb_if({clocks[0].get('name','clk')});
  {dut_name} dut(.{clocks[0].get('name','clk')}(tb_if.{clocks[0].get('name','clk')}));
  initial begin
    {clocks[0].get('name','clk')} = 0;
    run_test("uvm_test");
  end
endmodule
"""

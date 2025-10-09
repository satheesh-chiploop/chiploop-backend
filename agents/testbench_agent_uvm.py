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

        rtl_path = Path(state.get("artifact", workflow_dir / "auto_module.v"))
        spec_path = Path(state.get("spec_json", workflow_dir / "auto_module_spec.json"))
        log_path = workflow_dir / "testbench_agent_uvm_compile.log"

        if not rtl_path.exists():
            raise FileNotFoundError(f"RTL file not found: {rtl_path}")
        if not spec_path.exists():
            raise FileNotFoundError(f"Spec file not found: {spec_path}")

        rtl_text = rtl_path.read_text(encoding="utf-8")
        spec_json = json.loads(spec_path.read_text(encoding="utf-8"))

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

        # --- Base UVM components (unchanged) ---
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

        # --- LLM-generated enhancements ---
        llm_prompt = f"""
You are a verification engineer.
Generate SystemVerilog UVM stimulus ideas or assertions for the module below.

Module: {dut_name}
Clocks/Resets:
{domain_info}

Verilog RTL (snippet):
{rtl_text[:1500]}

Guidelines:
- Output only synthesizable SystemVerilog/UVM code fragments.
- Do not include ``` fences.
- Keep compatible with UVM 1.2.
"""

        extra_uvm = ""
        try:
            if USE_LOCAL_OLLAMA:
                print("⚙️ Using local Ollama to suggest UVM enhancements...")
                payload = {"model": "llama3", "prompt": llm_prompt}
                with requests.post(OLLAMA_URL, json=payload, stream=True, timeout=300) as r:
                    for line in r.iter_lines():
                        if not line:
                            continue
                        try:
                            j = json.loads(line.decode())
                            if "response" in j:
                                extra_uvm += j["response"]
                                print(j["response"], end="", flush=True)
                        except Exception:
                            continue
            else:
                print("🌐 Using Portkey backend to suggest UVM enhancements...")
                try:
                    completion = client_portkey.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=[{"role": "user", "content": llm_prompt}],
                        stream=True,
                    )
                    for chunk in completion:
                        if chunk and hasattr(chunk, "choices"):
                            delta = chunk.choices[0].delta.get("content", "")
                            if delta:
                                extra_uvm += delta
                                print(delta, end="", flush=True)
                except Exception as e:
                    print(f"⚠️ Portkey failed, falling back to Ollama: {e}")
                    payload = {"model": "llama3", "prompt": llm_prompt}
                    with requests.post(OLLAMA_URL, json=payload, stream=True, timeout=300) as r:
                        for line in r.iter_lines():
                            if not line:
                                continue
                            try:
                                j = json.loads(line.decode())
                                if "response" in j:
                                    extra_uvm += j["response"]
                            except Exception:
                                continue
        except Exception as e:
            extra_uvm = f"// ⚠️ LLM enhancement failed: {e}"

        # --- Write files ---
        files = {
            f"{dut_name}_env.sv": env,
            f"{dut_name}_drv.sv": drv,
            f"{dut_name}_mon.sv": mon,
            f"{dut_name}_seq.sv": seq,
            f"{dut_name}_if.sv": iface,
            f"tb_{dut_name}.sv": tb_top,
            f"{dut_name}_extra_uvm.sv": extra_uvm,
        }

        for fname, content in files.items():
            (tb_dir / fname).write_text(content, encoding="utf-8")

        with open(log_path, "w", encoding="utf-8") as logf:
            logf.write(f"UVM TB generated for {dut_name} at {datetime.datetime.now()}\n")
            logf.write(f"Clocks/Resets Context:\n{domain_info}\n")

        state.update({
            "status": "✅ UVM Testbench generated",
            "artifact": str(tb_dir),
            "artifact_log": str(log_path),
            "workflow_id": workflow_id,
            "workflow_dir": str(workflow_dir),
        })

        print(f"\n✅ UVM Testbench Agent completed — TB @ {tb_dir}")
        return state

    except Exception as e:
        logger.error(f"❌ UVM Testbench Agent failed: {e}")
        state["status"] = f"❌ Failed: {e}"
        return state

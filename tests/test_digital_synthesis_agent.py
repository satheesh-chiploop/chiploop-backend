import os

import pytest

os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from agents.digital import digital_synthesis_agent as agent


def test_repair_common_status_tieoffs_adds_safe_assignments(tmp_path):
    rtl = tmp_path / "top.v"
    rtl.write_text(
        "module top;\n"
        "wire status_packet_active;\n"
        "wire status_error;\n"
        "wire status_tx_busy;\n"
        "wire status_rx_busy;\n"
        "wire rx_overflow_event;\n"
        "wire tx_underflow_event;\n"
        "endmodule\n",
        encoding="utf-8",
    )

    repairs = agent._repair_common_status_tieoffs(str(rtl))
    text = rtl.read_text(encoding="utf-8")

    assert repairs == [
        "assign status_packet_active = status_tx_busy | status_rx_busy;",
        "assign status_error = rx_overflow_event | tx_underflow_event;",
    ]
    assert "assign status_packet_active = status_tx_busy | status_rx_busy;" in text
    assert "assign status_error = rx_overflow_event | tx_underflow_event;" in text


def test_repair_stale_input_only_interconnect_promotes_top_input(tmp_path):
    digital = tmp_path / "digital_core.v"
    analog = tmp_path / "analog_macro.v"
    top = tmp_path / "soc_top_phys.sv"
    digital.write_text("module digital_core(input sample_req); endmodule\n", encoding="utf-8")
    analog.write_text("module analog_macro(input sample_req); endmodule\n", encoding="utf-8")
    top.write_text(
        """
module soc_top_phys(input logic clk);
  logic w_4_u_digital_sample_req_u_analog_sample_req;
  digital_core u_digital (
    .sample_req(w_4_u_digital_sample_req_u_analog_sample_req)
  );
  analog_macro u_analog (
    .sample_req(w_4_u_digital_sample_req_u_analog_sample_req)
  );
endmodule
""".strip()
        + "\n",
        encoding="utf-8",
    )

    repairs = agent._repair_stale_input_only_interconnects(
        [str(digital), str(analog), str(top)],
        "soc_top_phys",
    )
    text = top.read_text(encoding="utf-8")

    assert repairs == {
        "soc_top_phys.sv": [
            "promoted input-only interconnect w_4_u_digital_sample_req_u_analog_sample_req to top input sample_req"
        ]
    }
    assert "input logic clk, input logic sample_req" in text
    assert "w_4_u_digital_sample_req_u_analog_sample_req" not in text
    assert ".sample_req(sample_req)" in text


def test_repair_stale_input_only_interconnect_reuses_existing_top_port(tmp_path):
    analog = tmp_path / "analog_macro.v"
    top = tmp_path / "soc_top_phys.sv"
    analog.write_text("module analog_macro(input sample_req); endmodule\n", encoding="utf-8")
    top.write_text(
        """
module soc_top_phys(output logic sample_req);
  logic w_4_u_digital_sample_req_u_analog_sample_req;
  analog_macro u_analog (
    .sample_req(w_4_u_digital_sample_req_u_analog_sample_req)
  );
endmodule
""".strip()
        + "\n",
        encoding="utf-8",
    )

    repairs = agent._repair_stale_input_only_interconnects(
        [str(analog), str(top)],
        "soc_top_phys",
    )
    text = top.read_text(encoding="utf-8")

    assert repairs == {
        "soc_top_phys.sv": [
            "reconnected input-only interconnect w_4_u_digital_sample_req_u_analog_sample_req to top port sample_req"
        ]
    }
    assert "module soc_top_phys(output logic sample_req);" in text
    assert "w_4_u_digital_sample_req_u_analog_sample_req" not in text
    assert ".sample_req(sample_req)" in text


def test_repair_mirrored_output_interconnects_connects_top_and_internal_mirrors(tmp_path):
    sampler = tmp_path / "sensor_hub_sampler.v"
    top = tmp_path / "smart_sensor_hub_mcu.v"
    sampler.write_text(
        """
module sensor_hub_sampler(
  output low_power_active,
  output [5:0] fifo_level_out,
  output [31:0] sample_count_out,
  output [7:0] alert_status_out
);
endmodule
""".strip()
        + "\n",
        encoding="utf-8",
    )
    top.write_text(
        """
module smart_sensor_hub_mcu (
  fifo_level,
  low_power_active,
  sample_count,
  alert_status
);
output [5:0] fifo_level;
output low_power_active;
output [31:0] sample_count;
output [7:0] alert_status;
wire sensor_hub_sampler_low_power_active_out;
wire [5:0] sensor_hub_sampler_fifo_level_out;
wire [31:0] sensor_hub_sampler_sample_count_out;
wire [7:0] sensor_hub_sampler_alert_status_out;
wire [5:0] register_file_fifo_level_in;
wire [31:0] register_file_sample_count_in;
wire [7:0] register_file_alert_status_in;

assign register_file_fifo_level_in = sensor_hub_sampler_fifo_level_out;
assign register_file_sample_count_in = sensor_hub_sampler_sample_count_out;
assign register_file_alert_status_in = sensor_hub_sampler_alert_status_out;

sensor_hub_sampler u_sensor_hub_sampler (
  .low_power_active(sensor_hub_sampler_low_power_active_out),
  .fifo_level_out(fifo_level),
  .sample_count_out(sample_count),
  .alert_status_out(alert_status)
);
endmodule
""".strip()
        + "\n",
        encoding="utf-8",
    )

    repairs = agent._repair_mirrored_output_interconnects(
        [str(sampler), str(top)],
        "smart_sensor_hub_mcu",
    )
    text = top.read_text(encoding="utf-8")

    assert repairs == {
        "smart_sensor_hub_mcu.v": [
            "connected top output low_power_active from mirrored output wire sensor_hub_sampler_low_power_active_out",
            "connected mirrored output wire sensor_hub_sampler_fifo_level_out from top output fifo_level",
            "connected mirrored output wire sensor_hub_sampler_sample_count_out from top output sample_count",
            "connected mirrored output wire sensor_hub_sampler_alert_status_out from top output alert_status",
        ]
    }
    assert "assign low_power_active = sensor_hub_sampler_low_power_active_out;" in text
    assert "assign sensor_hub_sampler_fifo_level_out = fifo_level;" in text
    assert "assign sensor_hub_sampler_sample_count_out = sample_count;" in text
    assert "assign sensor_hub_sampler_alert_status_out = alert_status;" in text


def test_regenerate_system_physical_top_from_intent_rewrites_stale_top(tmp_path):
    digital = tmp_path / "digital_core.v"
    analog = tmp_path / "analog_macro.v"
    top = tmp_path / "soc_top_phys.sv"
    digital.write_text(
        "module digital_core(input logic clk, output logic sample_req); endmodule\n",
        encoding="utf-8",
    )
    analog.write_text("module analog_macro(input logic sample_req); endmodule\n", encoding="utf-8")
    top.write_text(
        """
module soc_top_phys(input logic clk);
  logic w_4_u_digital_sample_req_u_analog_sample_req;
  digital_core u_digital (
    .clk(clk),
    .sample_req(sample_req)
  );
  analog_macro u_analog (
    .sample_req(w_4_u_digital_sample_req_u_analog_sample_req)
  );
endmodule
""".strip()
        + "\n",
        encoding="utf-8",
    )

    repairs = agent._regenerate_system_physical_top_from_intent(
        [str(digital), str(analog), str(top)],
        "soc_top_phys",
        {
            "system_integration_intent": {
                "instances": [
                    {"name": "u_digital", "module": "digital_core"},
                    {"name": "u_analog", "module": "analog_macro"},
                ],
                "connections": [
                    {"from": "top.clk", "to": "u_digital.clk"},
                    {"from": "u_digital.sample_req", "to": "u_analog.sample_req"},
                ],
            }
        },
    )
    text = top.read_text(encoding="utf-8")

    assert repairs == {"soc_top_phys.sv": ["regenerated physical top from system integration intent"]}
    assert "w_4_u_digital_sample_req_u_analog_sample_req" not in text
    assert "w_1_u_digital_sample_req_u_analog_sample_req" in text
    assert text.count(".sample_req(w_1_u_digital_sample_req_u_analog_sample_req)") == 2


def test_synthesis_uses_system_package_phys_top_and_fails_before_sta_on_missing_netlist(tmp_path, monkeypatch):
    rtl = tmp_path / "temp_monitor_soc_phys.sv"
    sdc = tmp_path / "top.sdc"
    rtl.write_text("module temp_monitor_soc_phys(input clk); endmodule\n", encoding="utf-8")
    sdc.write_text("create_clock -name clk -period 10 [get_ports clk]\n", encoding="utf-8")

    monkeypatch.setattr(agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")
    monkeypatch.setattr(agent, "_run", lambda *args, **kwargs: (2, "ERROR: Module `top' not found!\n"))

    with pytest.raises(RuntimeError, match="synthesis failed before downstream PD stages"):
        agent.run_agent({
            "workflow_id": "wf",
            "workflow_dir": str(tmp_path),
            "workflow_name": "System_Synthesis",
            "digital": {
                "top_module": "top",
                "rtl_files": [str(rtl)],
                "constraints_sdc": str(sdc),
            },
            "system_rtl_package": {
                "top": {"phys": "temp_monitor_soc_phys", "sim": "temp_monitor_soc_sim"},
            },
        })

    summary = (tmp_path / "digital" / "synth" / "synth_summary.json").read_text(encoding="utf-8")
    config = (tmp_path / "digital" / "synth" / "config.json").read_text(encoding="utf-8")
    assert '"top_module": "temp_monitor_soc_phys"' in summary
    assert '"DESIGN_NAME": "temp_monitor_soc_phys"' in config


def test_arch2synthesis_uses_digital_top_even_when_system_package_exists(tmp_path, monkeypatch):
    rtl = tmp_path / "digital_top.sv"
    sdc = tmp_path / "digital_top.sdc"
    rtl.write_text("module digital_top(input clk); endmodule\n", encoding="utf-8")
    sdc.write_text("create_clock -name clk -period 10 [get_ports clk]\n", encoding="utf-8")

    monkeypatch.setattr(agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")
    monkeypatch.setattr(agent, "_run", lambda *args, **kwargs: (2, "synthesis failed after selecting top\n"))

    with pytest.raises(RuntimeError, match="synthesis failed before downstream PD stages"):
        agent.run_agent({
            "workflow_id": "wf",
            "workflow_dir": str(tmp_path),
            "workflow_name": "Digital_Synthesis",
            "digital": {
                "top_module": "digital_top",
                "rtl_files": [str(rtl)],
                "constraints_sdc": str(sdc),
            },
            "system_rtl_package": {
                "top": {"phys": "temp_monitor_soc_phys", "sim": "temp_monitor_soc_sim"},
            },
        })

    summary = (tmp_path / "digital" / "synth" / "synth_summary.json").read_text(encoding="utf-8")
    config = (tmp_path / "digital" / "synth" / "config.json").read_text(encoding="utf-8")
    assert '"top_module": "digital_top"' in summary
    assert '"DESIGN_NAME": "digital_top"' in config


def test_synthesis_blocks_large_inferred_memory_without_macro_liberty(tmp_path, monkeypatch):
    sram = tmp_path / "sram_model.v"
    top = tmp_path / "top.v"
    sdc = tmp_path / "top.sdc"
    sram.write_text(
        """
module sram_model(input clk, input [7:0] addr, input [31:0] din, output reg [31:0] dout);
  reg [31:0] mem [0:255];
  always @(posedge clk) dout <= mem[addr];
endmodule
""",
        encoding="utf-8",
    )
    top.write_text(
        """
module top(input clk, input [7:0] addr, input [31:0] din, output [31:0] dout);
  sram_model u_sram(.clk(clk), .addr(addr), .din(din), .dout(dout));
endmodule
""",
        encoding="utf-8",
    )
    sdc.write_text("create_clock -name clk -period 10 [get_ports clk]\n", encoding="utf-8")
    monkeypatch.setattr(agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")
    monkeypatch.setattr(agent, "_run", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("OpenLane should not run")))

    with pytest.raises(RuntimeError, match="inferred_memory_macro_requires_real_macro_collateral"):
        agent.run_agent({
            "workflow_id": "wf",
            "workflow_dir": str(tmp_path),
            "workflow_name": "Digital_Arch2Synthesis",
            "digital": {
                "top_module": "top",
                "rtl_files": [str(sram), str(top)],
                "constraints_sdc": str(sdc),
            },
        })

    summary = (tmp_path / "digital" / "synth" / "synth_summary.json").read_text(encoding="utf-8")
    metrics = (tmp_path / "digital" / "synth" / "metrics.json").read_text(encoding="utf-8")
    assert '"status": "blocked"' in summary
    assert "inferred_memory_macro_requires_real_macro_collateral" in summary
    assert '"inferred_memory_bit_count": 8192' in metrics


def test_synthesis_binds_inferred_sram_to_matching_pdk_macro(tmp_path, monkeypatch):
    sram = tmp_path / "sram_model.v"
    top = tmp_path / "top.v"
    sdc = tmp_path / "top.sdc"
    pdk = tmp_path / "pdk" / "sky130A" / "libs.ref" / "sky130_sram_macros"
    for subdir in ("lib", "lef", "gds", "spice", "verilog"):
        (pdk / subdir).mkdir(parents=True, exist_ok=True)
    stem = "sky130_sram_1kbyte_1rw1r_32x256_8"
    (pdk / "lib" / f"{stem}_TT_1p8V_25C.lib").write_text(
        f"""
library(test) {{
  type(DATA) {{ bit_from : 31; bit_to : 0; }}
  type(ADDR) {{ bit_from : 7; bit_to : 0; }}
  type(MASK) {{ bit_from : 3; bit_to : 0; }}
  cell({stem}) {{
    area: 1;
    pin(clk0) {{ direction : input; }}
    pin(csb0) {{ direction : input; }}
    pin(web0) {{ direction : input; }}
    bus(wmask0) {{ bus_type : MASK; direction : input; }}
    bus(addr0) {{ bus_type : ADDR; direction : input; }}
    bus(din0) {{ bus_type : DATA; direction : input; }}
    bus(dout0) {{ bus_type : DATA; direction : output; }}
  }}
}}
""",
        encoding="utf-8",
    )
    (pdk / "lef" / f"{stem}.lef").write_text(f"MACRO {stem}\nEND {stem}\n", encoding="utf-8")
    (pdk / "gds" / f"{stem}.gds").write_text("gds\n", encoding="utf-8")
    (pdk / "spice" / f"{stem}.spice").write_text(f".subckt {stem} clk0 csb0 web0 wmask0 addr0 din0 dout0\n.ends\n", encoding="utf-8")
    (pdk / "verilog" / f"{stem}.v").write_text(
        f"""
module {stem}(
  input clk0,
  input csb0,
  input web0,
  input [3:0] wmask0,
  input [7:0] addr0,
  input [31:0] din0,
  output [31:0] dout0
  , inout vccd1
  , inout vssd1
);
endmodule
""",
        encoding="utf-8",
    )
    sram.write_text(
        """
module sram_model(input clk, input csb, input web, input [7:0] addr, input [31:0] din, output reg [31:0] dout);
  reg [31:0] mem [0:255];
  always @(posedge clk) dout <= mem[addr];
endmodule
""",
        encoding="utf-8",
    )
    top.write_text(
        """
module top(input clk, input csb, input web, input [7:0] addr, input [31:0] din, output [31:0] dout);
  sram_model u_sram(.clk(clk), .csb(csb), .web(web), .addr(addr), .din(din), .dout(dout));
endmodule
""",
        encoding="utf-8",
    )
    sdc.write_text("create_clock -name clk -period 10 [get_ports clk]\n", encoding="utf-8")

    monkeypatch.setattr(agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")
    monkeypatch.setattr(agent, "_run", lambda *args, **kwargs: (2, "stopped after config for test\n"))

    with pytest.raises(RuntimeError, match="synthesis failed before downstream PD stages"):
        agent.run_agent({
            "workflow_id": "wf",
            "workflow_dir": str(tmp_path),
            "workflow_name": "Digital_Arch2Synthesis",
            "pdk_root_host": str(tmp_path / "pdk"),
            "pdk_variant": "sky130A",
            "digital": {
                "top_module": "top",
                "rtl_files": [str(sram), str(top)],
                "constraints_sdc": str(sdc),
            },
        })

    copied_sram = (tmp_path / "digital" / "synth" / "rtl" / "sram_model.v").read_text(encoding="utf-8")
    config = (tmp_path / "digital" / "synth" / "config.json").read_text(encoding="utf-8")
    summary = (tmp_path / "digital" / "synth" / "synth_summary.json").read_text(encoding="utf-8")
    assert "reg [31:0] mem [0:255]" not in copied_sram
    assert "module sram_model (clk, csb, web, addr, din, dout);" in copied_sram
    assert "output [31:0] dout;" in copied_sram
    assert f"{stem} u_chiploop_sram_macro" in copied_sram
    assert ".wmask0(4'b1111)" in copied_sram
    assert ".vssd1(" not in copied_sram
    assert ".vccd1(" not in copied_sram
    assert f'"dir::macro_libs/{stem}_TT_1p8V_25C.lib"' in config
    assert f'"dir::macro_lefs/{stem}.lef"' in config
    assert f'"dir::macro_gds/{stem}.gds"' in config
    assert f'"macro_name": "{stem}"' in summary

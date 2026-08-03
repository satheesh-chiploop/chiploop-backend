import json
import math
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict


def _sincos_source() -> str:
    def literal(value: int) -> str:
        return f"-16'sd{abs(value)}" if value < 0 else f"16'sd{value}"

    cases = []
    for index in range(256):
        angle = 2.0 * math.pi * index / 256.0
        sin_value = max(-32768, min(32767, round(math.sin(angle) * 32767)))
        cos_value = max(-32768, min(32767, round(math.cos(angle) * 32767)))
        cases.append(f"      8'd{index}: begin sin_q15 = {literal(sin_value)}; cos_q15 = {literal(cos_value)}; end")
    return """module sincos_lut (
  input  logic [15:0] angle_turns,
  output logic signed [15:0] sin_q15,
  output logic signed [15:0] cos_q15
);
  always_comb begin
    unique case (angle_turns[15:8])
%s
      default: begin sin_q15 = 16'sd0; cos_q15 = 16'sd32767; end
    endcase
  end
endmodule
""" % "\n".join(cases)


CLARKE = r"""module clarke_transform (
  input logic signed [15:0] phase_a, phase_b,
  output logic signed [15:0] alpha, beta
);
  logic signed [17:0] sum_ab;
  logic signed [33:0] beta_product;
  function automatic signed [15:0] sat16(input signed [33:0] value);
    if (value > 32767) sat16 = 16'sh7fff;
    else if (value < -32768) sat16 = -16'sd32768;
    else sat16 = value[15:0];
  endfunction
  always_comb begin
    alpha = phase_a;
    sum_ab = $signed(phase_a) + ($signed(phase_b) <<< 1);
    beta_product = sum_ab * 16'sd18919;
    beta = sat16(beta_product >>> 15);
  end
endmodule
"""


PARK = r"""module park_transform (
  input logic signed [15:0] alpha, beta, sin_q15, cos_q15,
  output logic signed [15:0] direct, quadrature
);
  logic signed [32:0] direct_sum, quadrature_sum;
  function automatic signed [15:0] sat16(input signed [32:0] value);
    if (value > 32767) sat16 = 16'sh7fff;
    else if (value < -32768) sat16 = -16'sd32768;
    else sat16 = value[15:0];
  endfunction
  always_comb begin
    direct_sum = $signed(alpha) * $signed(cos_q15) + $signed(beta) * $signed(sin_q15);
    quadrature_sum = -($signed(alpha) * $signed(sin_q15)) + $signed(beta) * $signed(cos_q15);
    direct = sat16(direct_sum >>> 15);
    quadrature = sat16(quadrature_sum >>> 15);
  end
endmodule
"""


INVERSE_PARK = r"""module inverse_park_transform (
  input logic signed [15:0] direct, quadrature, sin_q15, cos_q15,
  output logic signed [15:0] alpha, beta
);
  logic signed [32:0] alpha_sum, beta_sum;
  function automatic signed [15:0] sat16(input signed [32:0] value);
    if (value > 32767) sat16 = 16'sh7fff;
    else if (value < -32768) sat16 = -16'sd32768;
    else sat16 = value[15:0];
  endfunction
  always_comb begin
    alpha_sum = $signed(direct) * $signed(cos_q15) - $signed(quadrature) * $signed(sin_q15);
    beta_sum = $signed(direct) * $signed(sin_q15) + $signed(quadrature) * $signed(cos_q15);
    alpha = sat16(alpha_sum >>> 15);
    beta = sat16(beta_sum >>> 15);
  end
endmodule
"""


PI_CONTROLLER = r"""module pi_controller #(
  parameter signed [15:0] KP_Q15 = 16'sd4096,
  parameter signed [15:0] KI_Q15 = 16'sd128,
  parameter signed [15:0] OUT_MIN = -16'sd32768,
  parameter signed [15:0] OUT_MAX = 16'sd32767
) (
  input logic clk, reset_n, sample_valid, clear_integrator,
  input logic signed [15:0] reference_value, measured_value,
  output logic signed [15:0] command
);
  logic signed [16:0] error;
  logic signed [47:0] integrator, next_integrator;
  logic signed [32:0] proportional;
  logic signed [47:0] candidate;
  localparam signed [47:0] INTEGRATOR_MAX = 48'sd1073709056;
  localparam signed [47:0] INTEGRATOR_MIN = -48'sd1073741824;
  always_comb begin
    error = $signed(reference_value) - $signed(measured_value);
    proportional = error * KP_Q15;
    next_integrator = integrator + error * KI_Q15;
    if (next_integrator > INTEGRATOR_MAX) next_integrator = INTEGRATOR_MAX;
    else if (next_integrator < INTEGRATOR_MIN) next_integrator = INTEGRATOR_MIN;
    candidate = proportional + next_integrator;
  end
  always_ff @(posedge clk or negedge reset_n) begin
    if (!reset_n) begin integrator <= '0; command <= '0; end
    else if (clear_integrator) begin integrator <= '0; command <= '0; end
    else if (sample_valid) begin
      integrator <= next_integrator;
      if ((candidate >>> 15) > OUT_MAX) command <= OUT_MAX;
      else if ((candidate >>> 15) < OUT_MIN) command <= OUT_MIN;
      else command <= candidate >>> 15;
    end
  end
endmodule
"""


SVPWM = r"""module svpwm_duty (
  input logic clk, reset_n,
  input logic signed [15:0] alpha_q15, beta_q15,
  input logic force_off,
  output logic [15:0] duty_u, duty_v, duty_w,
  output logic pwm_u, pwm_v, pwm_w
);
  logic signed [32:0] sqrt_beta;
  logic signed [16:0] phase_u, phase_v, phase_w;
  logic [15:0] carrier;
  function automatic [15:0] duty_from_phase(input signed [16:0] phase);
    logic signed [17:0] shifted;
    begin
      shifted = phase + 18'sd32768;
      if (shifted < 0) duty_from_phase = 16'd0;
      else if (shifted > 65535) duty_from_phase = 16'hffff;
      else duty_from_phase = shifted[15:0];
    end
  endfunction
  always_comb begin
    sqrt_beta = $signed(beta_q15) * 16'sd28378;
    phase_u = alpha_q15;
    phase_v = (-$signed(alpha_q15) + (sqrt_beta >>> 14)) >>> 1;
    phase_w = (-$signed(alpha_q15) - (sqrt_beta >>> 14)) >>> 1;
    duty_u = force_off ? 16'h8000 : duty_from_phase(phase_u);
    duty_v = force_off ? 16'h8000 : duty_from_phase(phase_v);
    duty_w = force_off ? 16'h8000 : duty_from_phase(phase_w);
  end
  always_ff @(posedge clk or negedge reset_n) begin
    if (!reset_n) carrier <= '0;
    else carrier <= carrier + 1'b1;
  end
  always_comb begin
    pwm_u = !force_off && (carrier < duty_u);
    pwm_v = !force_off && (carrier < duty_v);
    pwm_w = !force_off && (carrier < duty_w);
  end
endmodule
"""


FAULT_MONITOR = r"""module motor_fault_monitor #(
  parameter signed [15:0] CURRENT_LIMIT_RAW = 16'sd30720
) (
  input logic clk, reset_n, clear_fault, external_fault,
  input logic signed [15:0] phase_current_a, phase_current_b,
  output logic fault
);
  logic [15:0] abs_a, abs_b;
  logic [16:0] current_sum;
  always_comb begin
    abs_a = phase_current_a[15] ? (~phase_current_a + 1'b1) : phase_current_a;
    abs_b = phase_current_b[15] ? (~phase_current_b + 1'b1) : phase_current_b;
    current_sum = abs_a + abs_b;
  end
  always_ff @(posedge clk or negedge reset_n) begin
    if (!reset_n) fault <= 1'b0;
    else if (clear_fault) fault <= 1'b0;
    else if (external_fault || current_sum > CURRENT_LIMIT_RAW) fault <= 1'b1;
  end
endmodule
"""


TOP = r"""module motor_control_top (
  input logic clk, reset_n, sample_valid,
  input logic signed [15:0] phase_current_a, phase_current_b,
  input logic [15:0] rotor_position_turns,
  input logic signed [15:0] speed_reference_rpm, speed_measured_rpm,
  input logic signed [15:0] dc_bus_voltage_v,
  input logic clear_fault, external_fault,
  output logic command_valid,
  output logic [15:0] duty_u, duty_v, duty_w,
  output logic pwm_u, pwm_v, pwm_w,
  output logic fault
);
  logic signed [15:0] sin_theta, cos_theta, i_alpha, i_beta, i_d, i_q;
  logic signed [15:0] iq_reference, vd_command, vq_command, v_alpha, v_beta;
  logic valid_d1, valid_d2;
  sincos_lut lut(.angle_turns(rotor_position_turns), .sin_q15(sin_theta), .cos_q15(cos_theta));
  clarke_transform clarke(.phase_a(phase_current_a), .phase_b(phase_current_b), .alpha(i_alpha), .beta(i_beta));
  park_transform park(.alpha(i_alpha), .beta(i_beta), .sin_q15(sin_theta), .cos_q15(cos_theta), .direct(i_d), .quadrature(i_q));
  pi_controller #(.KP_Q15(16'sd64), .KI_Q15(16'sd2), .OUT_MIN(-16'sd30720), .OUT_MAX(16'sd30720)) speed_pi(
    .clk(clk), .reset_n(reset_n), .sample_valid(sample_valid), .clear_integrator(fault),
    .reference_value(speed_reference_rpm), .measured_value(speed_measured_rpm), .command(iq_reference));
  pi_controller #(.KP_Q15(16'sd8192), .KI_Q15(16'sd256)) id_pi(
    .clk(clk), .reset_n(reset_n), .sample_valid(sample_valid), .clear_integrator(fault),
    .reference_value(16'sd0), .measured_value(i_d), .command(vd_command));
  pi_controller #(.KP_Q15(16'sd8192), .KI_Q15(16'sd256)) iq_pi(
    .clk(clk), .reset_n(reset_n), .sample_valid(sample_valid), .clear_integrator(fault),
    .reference_value(iq_reference), .measured_value(i_q), .command(vq_command));
  inverse_park_transform inverse_park(.direct(vd_command), .quadrature(vq_command), .sin_q15(sin_theta), .cos_q15(cos_theta), .alpha(v_alpha), .beta(v_beta));
  motor_fault_monitor faults(.clk(clk), .reset_n(reset_n), .clear_fault(clear_fault), .external_fault(external_fault), .phase_current_a(phase_current_a), .phase_current_b(phase_current_b), .fault(fault));
  svpwm_duty pwm(.clk(clk), .reset_n(reset_n), .alpha_q15(v_alpha), .beta_q15(v_beta), .force_off(fault), .duty_u(duty_u), .duty_v(duty_v), .duty_w(duty_w), .pwm_u(pwm_u), .pwm_v(pwm_v), .pwm_w(pwm_w));
  always_ff @(posedge clk or negedge reset_n) begin
    if (!reset_n) begin valid_d1 <= 1'b0; valid_d2 <= 1'b0; command_valid <= 1'b0; end
    else begin valid_d1 <= sample_valid; valid_d2 <= valid_d1; command_valid <= valid_d2; end
  end
endmodule
"""


MMIO_WRAPPER = r"""module motor_control_mmio_top (
  input logic clk, reset_n,
  input logic csr_valid, csr_write,
  input logic [7:0] csr_addr,
  input logic [31:0] csr_wdata,
  output logic csr_ready,
  output logic [31:0] csr_rdata,
  input logic sample_valid, external_fault,
  input logic signed [15:0] phase_current_a, phase_current_b,
  input logic [15:0] rotor_position_turns,
  input logic signed [15:0] speed_measured_rpm,
  input logic signed [15:0] dc_bus_voltage_v,
  output logic command_valid,
  output logic [15:0] duty_u, duty_v, duty_w,
  output logic pwm_u, pwm_v, pwm_w,
  output logic fault
);
  logic enable;
  logic clear_fault;
  logic signed [15:0] speed_reference_rpm;
  always_ff @(posedge clk or negedge reset_n) begin
    if (!reset_n) begin
      enable <= 1'b0;
      clear_fault <= 1'b0;
      speed_reference_rpm <= '0;
    end else begin
      clear_fault <= 1'b0;
      if (csr_valid && csr_write) begin
        case (csr_addr)
          8'h00: begin enable <= csr_wdata[0]; clear_fault <= csr_wdata[1]; end
          8'h04: speed_reference_rpm <= csr_wdata[15:0];
          default: begin end
        endcase
      end
    end
  end
  always_comb begin
    csr_ready = csr_valid;
    case (csr_addr)
      8'h00: csr_rdata = {30'b0, 1'b0, enable};
      8'h04: csr_rdata = {{16{speed_reference_rpm[15]}}, speed_reference_rpm};
      8'h08: csr_rdata = {30'b0, command_valid, fault};
      8'h0c: csr_rdata = {{16{speed_measured_rpm[15]}}, speed_measured_rpm};
      8'h10: csr_rdata = {{16{phase_current_a[15]}}, phase_current_a};
      8'h14: csr_rdata = {{16{phase_current_b[15]}}, phase_current_b};
      8'h18: csr_rdata = {16'b0, duty_u};
      8'h1c: csr_rdata = {16'b0, duty_v};
      8'h20: csr_rdata = {16'b0, duty_w};
      8'h24: csr_rdata = {{16{dc_bus_voltage_v[15]}}, dc_bus_voltage_v};
      8'h28: csr_rdata = {16'b0, rotor_position_turns};
      default: csr_rdata = 32'b0;
    endcase
  end
  motor_control_top core(
    .clk(clk), .reset_n(reset_n), .sample_valid(sample_valid),
    .phase_current_a(phase_current_a), .phase_current_b(phase_current_b),
    .rotor_position_turns(rotor_position_turns), .speed_reference_rpm(speed_reference_rpm),
    .speed_measured_rpm(speed_measured_rpm), .dc_bus_voltage_v(dc_bus_voltage_v),
    .clear_fault(clear_fault), .external_fault(external_fault || !enable),
    .command_valid(command_valid), .duty_u(duty_u), .duty_v(duty_v), .duty_w(duty_w),
    .pwm_u(pwm_u), .pwm_v(pwm_v), .pwm_w(pwm_w), .fault(fault));
endmodule
"""


TESTBENCH = r"""`timescale 1ns/1ps
module tb_motor_control;
  logic clk = 0, reset_n = 0, sample_valid = 0, clear_fault = 0, external_fault = 0;
  logic signed [15:0] ia = 0, ib = 0, speed_ref = 0, speed_meas = 0;
  logic [15:0] angle = 0, duty_u, duty_v, duty_w;
  logic command_valid, pwm_u, pwm_v, pwm_w, fault;
  logic signed [15:0] alpha, beta, sin_q15, cos_q15, direct, quadrature;
  always #10 clk = ~clk;
  sincos_lut lut(.angle_turns(angle), .sin_q15(sin_q15), .cos_q15(cos_q15));
  clarke_transform c(.phase_a(ia), .phase_b(ib), .alpha(alpha), .beta(beta));
  park_transform p(.alpha(alpha), .beta(beta), .sin_q15(sin_q15), .cos_q15(cos_q15), .direct(direct), .quadrature(quadrature));
  motor_control_top dut(.clk(clk), .reset_n(reset_n), .sample_valid(sample_valid), .phase_current_a(ia), .phase_current_b(ib), .rotor_position_turns(angle), .speed_reference_rpm(speed_ref), .speed_measured_rpm(speed_meas), .dc_bus_voltage_v(16'sd24576), .clear_fault(clear_fault), .external_fault(external_fault), .command_valid(command_valid), .duty_u(duty_u), .duty_v(duty_v), .duty_w(duty_w), .pwm_u(pwm_u), .pwm_v(pwm_v), .pwm_w(pwm_w), .fault(fault));
  initial begin
    repeat (3) @(posedge clk); reset_n <= 1;
    ia <= 16'sd2048; ib <= -16'sd1024; angle <= 16'd0; speed_ref <= 16'sd24000;
    sample_valid <= 1; @(posedge clk); sample_valid <= 0;
    #1;
    if (alpha !== 16'sd2048) $fatal(1, "Clarke alpha mismatch: %0d", alpha);
    if (beta > 16'sd1 || beta < -16'sd1) $fatal(1, "Clarke beta mismatch: %0d", beta);
    if (direct < 16'sd2047 || direct > 16'sd2048) $fatal(1, "Park direct mismatch: %0d", direct);
    repeat (2) @(posedge clk); #1;
    if (!command_valid) $fatal(1, "command_valid missing");
    if (^duty_u === 1'bx || ^duty_v === 1'bx || ^duty_w === 1'bx) $fatal(1, "unknown duty output");
    external_fault <= 1; @(posedge clk); external_fault <= 0; @(posedge clk);
    if (!fault) $fatal(1, "fault did not latch");
    if (pwm_u || pwm_v || pwm_w) $fatal(1, "PWM not disabled by fault");
    $display("MOTOR_RTL_SMOKE_PASS"); $finish;
  end
endmodule
"""


def generate_motor_rtl(payload: Dict[str, Any], output_dir: Path, rtl_contract: Dict[str, Any]) -> Dict[str, Any]:
    rtl_dir = output_dir / "rtl"
    rtl_dir.mkdir(parents=True, exist_ok=True)
    sources = {
        "sincos_lut.sv": _sincos_source(),
        "clarke_transform.sv": CLARKE,
        "park_transform.sv": PARK,
        "inverse_park_transform.sv": INVERSE_PARK,
        "pi_controller.sv": PI_CONTROLLER,
        "svpwm_duty.sv": SVPWM,
        "motor_fault_monitor.sv": FAULT_MONITOR,
        "motor_control_top.sv": TOP,
        "motor_control_mmio_top.sv": MMIO_WRAPPER,
        "tb_motor_control.sv": TESTBENCH,
    }
    for name, source in sources.items():
        (rtl_dir / name).write_text(source, encoding="utf-8")

    compile_result: Dict[str, Any] = {"tool": "iverilog", "available": bool(shutil.which("iverilog")), "compiled": False, "smoke_passed": False}
    if compile_result["available"]:
        output = rtl_dir / "motor_control_tb.vvp"
        command = ["iverilog", "-g2012", "-s", "tb_motor_control", "-o", str(output)] + [str(rtl_dir / name) for name in sources]
        compile_process = subprocess.run(command, capture_output=True, text=True, timeout=60)
        compile_result.update({"compiled": compile_process.returncode == 0, "compile_stdout": compile_process.stdout[-4000:], "compile_stderr": compile_process.stderr[-4000:]})
        if compile_process.returncode == 0:
            run_process = subprocess.run(["vvp", str(output)], capture_output=True, text=True, timeout=30)
            compile_result.update({"smoke_passed": run_process.returncode == 0 and "MOTOR_RTL_SMOKE_PASS" in run_process.stdout, "run_stdout": run_process.stdout[-4000:], "run_stderr": run_process.stderr[-4000:]})
    manifest = {
        "schema": "chiploop.physical_ai.motor_rtl_package.v1",
        "top_module": "motor_control_top",
        "firmware_top_module": "motor_control_mmio_top",
        "sources": [name for name in sources if name != "tb_motor_control.sv"],
        "testbench": "tb_motor_control.sv",
        "numeric_contract": rtl_contract,
        "verification": compile_result,
        "status": "smoke_verified" if compile_result["smoke_passed"] else "generated_unverified",
        "limitations": [
            "Controller gains are safe baseline values and require plant-specific tuning before driving a motor.",
            "The SVPWM stage is a synthesizable sinusoidal duty generator; dead-time insertion belongs in the board-specific gate-driver layer.",
            "Smoke verification covers transform identity, valid propagation, known duty outputs, and hard-fault shutdown; closed-loop equivalence is pending.",
        ],
    }
    manifest_path = rtl_dir / "motor_rtl_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    files = {f"rtl_{Path(name).stem}": str(rtl_dir / name) for name in sources}
    files["motor_rtl_manifest"] = str(manifest_path)
    return {"manifest": manifest, "files": files}

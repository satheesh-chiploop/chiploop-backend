import os
import re
from typing import Any

from .fpga_common import fpga_dir, manifest_update, publish_json, write_text


_CLOCK_NAMES = {"clk", "clock", "core_clk", "sys_clk"}
_RESET_LOW_NAMES = {"reset_n", "rst_n", "aresetn"}
_RESET_HIGH_NAMES = {"reset", "rst", "areset"}


def _top_ports(paths: list[str], top: str) -> list[dict[str, Any]]:
    for path in paths:
        try:
            text = open(path, "r", encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        match = re.search(
            rf"\bmodule\s+{re.escape(top)}\s*\((?P<header>.*?)\)\s*;(?P<body>.*?)\bendmodule\b",
            text,
            flags=re.DOTALL,
        )
        if not match:
            continue
        header = match.group("header")
        body = match.group("body")
        ports_by_name: dict[str, dict[str, Any]] = {}
        header_order: list[str] = []
        direction = ""
        width_text = ""
        width = 1
        for segment in header.split(","):
            direction_match = re.search(r"\b(input|output|inout)\b", segment)
            if direction_match:
                direction = direction_match.group(1)
                width_match = re.search(r"\[\s*(\d+)\s*:\s*(\d+)\s*\]", segment)
                if width_match:
                    high, low = int(width_match.group(1)), int(width_match.group(2))
                    width_text = f"[{high}:{low}]"
                    width = abs(high - low) + 1
                else:
                    width_text, width = "", 1
            clean = re.sub(r"\[[^\]]+\]", " ", segment)
            tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_$]*", clean)
            useful = [token for token in tokens if token not in {"input", "output", "inout", "wire", "reg", "logic", "signed", "unsigned"}]
            if useful:
                name = useful[-1]
                header_order.append(name)
                if direction:
                    ports_by_name[name] = {"name": name, "direction": direction, "width": width, "range": width_text}

        # Non-ANSI Verilog lists only names in the module header and declares
        # direction/width in the body.  This form is common in generated RTL.
        for declaration in re.finditer(
            r"^\s*(?P<direction>input|output|inout)\b\s*"
            r"(?:wire\s*|reg\s*|logic\s*)?(?:signed\s*)?"
            r"(?P<range>\[\s*\d+\s*:\s*\d+\s*\]\s*)?(?P<names>[^;]+);",
            body,
            flags=re.MULTILINE,
        ):
            range_text = (declaration.group("range") or "").strip()
            range_match = re.search(r"\[\s*(\d+)\s*:\s*(\d+)\s*\]", range_text)
            declared_width = abs(int(range_match.group(1)) - int(range_match.group(2))) + 1 if range_match else 1
            for raw_name in declaration.group("names").split(","):
                names = re.findall(r"[A-Za-z_][A-Za-z0-9_$]*", re.sub(r"=.*", "", raw_name))
                if not names:
                    continue
                name = names[-1]
                if name in header_order:
                    ports_by_name[name] = {
                        "name": name,
                        "direction": declaration.group("direction"),
                        "width": declared_width,
                        "range": range_text,
                    }
        ports = [ports_by_name[name] for name in header_order if name in ports_by_name]
        if ports:
            return ports
    return []


def _decl(kind: str, port: dict[str, Any]) -> str:
    span = f" {port['range']}" if port.get("range") else ""
    return f"  {kind}{span} core_{port['name']};"


def _shift_expression(name: str, width: int, bit: str) -> str:
    return bit if width == 1 else f"{{{name}[{width - 2}:0], {bit}}}"


def add_spi_transport_if_needed(state: dict, *, threshold_bits: int = 64) -> dict | None:
    """Add an FPGA-only SPI shell when the core's parallel interface is too wide."""
    fpga = state.get("fpga") if isinstance(state.get("fpga"), dict) else {}
    rtl_files = [str(path) for path in fpga.get("rtl_files") or []]
    core_top = str(fpga.get("top_module") or state.get("top_module") or "")
    ports = _top_ports(rtl_files, core_top)
    total_bits = sum(int(port["width"]) for port in ports)
    if not ports or total_bits <= threshold_bits or state.get("auto_serialize_wide_io") is False:
        return None
    if any(port["direction"] == "inout" for port in ports):
        return {"status": "not_generated", "reason": "inout_ports_require_explicit_board_adapter", "core_top_module": core_top}

    payload_inputs = [port for port in ports if port["direction"] == "input" and port["name"].lower() not in _CLOCK_NAMES | _RESET_LOW_NAMES | _RESET_HIGH_NAMES]
    payload_outputs = [port for port in ports if port["direction"] == "output"]
    input_bits = max(1, sum(int(port["width"]) for port in payload_inputs))
    output_bits = max(1, sum(int(port["width"]) for port in payload_outputs))
    wrapper_top = f"{core_top}_spi_fpga_top"
    lines = [
        "// Auto-generated FPGA-only serialized transport shell.",
        "// The verified core RTL remains unchanged; ASIC flows continue to use the core top.",
        f"module {wrapper_top} (",
        "  input  logic clk,",
        "  input  logic reset_n,",
        "  input  logic spi_sclk,",
        "  input  logic spi_cs_n,",
        "  input  logic spi_mosi,",
        "  output logic spi_miso,",
        "  output logic fault_indicator",
        ");",
        f"  localparam integer INPUT_BITS = {input_bits};",
        f"  localparam integer OUTPUT_BITS = {output_bits};",
        "  logic [INPUT_BITS-1:0] rx_shift, rx_active;",
        "  logic [OUTPUT_BITS-1:0] tx_shift, tx_snapshot;",
        "  logic spi_active;",
        "  logic spi_cs_meta, spi_cs_sync, spi_cs_prev;",
    ]
    lines.extend(_decl("logic", port) for port in payload_inputs)
    lines.extend(_decl("wire", port) for port in payload_outputs)
    offset = 0
    for port in payload_inputs:
        width = int(port["width"])
        lines.append(f"  assign core_{port['name']} = rx_active[{offset} +: {width}];")
        offset += width
    response_items = [f"core_{port['name']}" for port in payload_outputs]
    response = "{" + ", ".join(response_items) + "}" if len(response_items) > 1 else (response_items[0] if response_items else "1'b0")
    lines.append(f"  wire [OUTPUT_BITS-1:0] core_response = {response};")
    fault = next((port for port in payload_outputs if port["name"].lower() in {"fault", "fault_flag", "error", "error_flag"}), None)
    fault_signal = f"core_{fault['name']}" if fault else "1'b0"
    lines.append(f"  assign fault_indicator = {fault_signal};")
    zero_bit = "1'b0"
    lines.extend([
        "  // Chip select asynchronously clears only the frame-state bit. Data",
        "  // registers use SPI clock alone, which is legal in ECP5 fabric.",
        "  always_ff @(posedge spi_sclk or posedge spi_cs_n) begin",
        "    if (spi_cs_n) spi_active <= 1'b0;",
        "    else spi_active <= 1'b1;",
        "  end",
        "  always_ff @(posedge spi_sclk) begin",
        "    if (!spi_cs_n) begin",
        f"      rx_shift <= {_shift_expression('rx_shift', input_bits, 'spi_mosi')};",
        f"      if (!spi_active) tx_shift <= {_shift_expression('tx_snapshot', output_bits, zero_bit)};",
        f"      else tx_shift <= {_shift_expression('tx_shift', output_bits, zero_bit)};",
        "    end",
        "  end",
        "  // Synchronize frame completion into the core clock domain. The host",
        "  // keeps MOSI stable around CS rising as required by the protocol.",
        "  always_ff @(posedge clk or negedge reset_n) begin",
        "    if (!reset_n) begin",
        "      spi_cs_meta <= 1'b1; spi_cs_sync <= 1'b1; spi_cs_prev <= 1'b1;",
        "      rx_active <= '0; tx_snapshot <= '0;",
        "    end else begin",
        "      spi_cs_meta <= spi_cs_n; spi_cs_sync <= spi_cs_meta; spi_cs_prev <= spi_cs_sync;",
        "      if (spi_cs_sync && !spi_cs_prev) rx_active <= rx_shift;",
        "      tx_snapshot <= core_response;",
        "    end",
        "  end",
        "  always_comb spi_miso = spi_active ? tx_shift[OUTPUT_BITS-1] : tx_snapshot[OUTPUT_BITS-1];",
        f"  {core_top} u_core (",
    ])
    connections = []
    for port in ports:
        lower = port["name"].lower()
        signal = "clk" if lower in _CLOCK_NAMES else "reset_n" if lower in _RESET_LOW_NAMES else "~reset_n" if lower in _RESET_HIGH_NAMES else f"core_{port['name']}"
        connections.append(f"    .{port['name']}({signal})")
    lines.append(",\n".join(connections))
    lines.extend(["  );", "endmodule", ""])

    out_dir = fpga_dir(state, "target_explorer", "interface_adapter")
    # Yosys executes from a board/strategy-specific working directory. Keep
    # generated RTL absolute so every candidate reads the same wrapper.
    wrapper_path = os.path.abspath(
        write_text(os.path.join(out_dir, f"{wrapper_top}.sv"), "\n".join(lines))
    )
    workflow_id = str(state.get("workflow_id") or "")
    if workflow_id:
        try:
            from utils.artifact_utils import save_text_artifact_and_record

            save_text_artifact_and_record(
                workflow_id,
                "FPGA Explorer I/O Mapping Agent",
                "fpga/target_explorer/interface_adapter",
                os.path.basename(wrapper_path),
                "\n".join(lines),
            )
        except Exception:
            pass
    adapted_files = [*rtl_files, wrapper_path]
    manifest_update(state, "core_top_module", core_top)
    manifest_update(state, "top_module", wrapper_top)
    manifest_update(state, "rtl_files", adapted_files)
    state["top_module"] = wrapper_top
    report = {
        "status": "generated", "transport": "spi_mode_0_shift_transport",
        "core_top_module": core_top, "fpga_top_module": wrapper_top,
        "original_top_level_io_bits": total_bits, "fpga_top_level_io_bits": 7,
        "serialized_input_bits": input_bits, "serialized_output_bits": output_bits,
        "input_bit_map": [
            {"port": port["name"], "width": int(port["width"]), "lsb": sum(int(item["width"]) for item in payload_inputs[:index])}
            for index, port in enumerate(payload_inputs)
        ],
        "output_frame_order_msb_first": [port["name"] for port in payload_outputs],
        "transaction_model": "MSB-first full-duplex frames; command N is committed when CS rises and response N is read in the following frame.",
        "wrapper_rtl": wrapper_path,
        "scope": "FPGA only; ASIC uses the original core top module.",
        "hardware_readiness": "requires CDC/protocol verification and board pin assignment before programming hardware",
    }
    publish_json(state, "FPGA Explorer I/O Mapping Agent", "target_explorer", "fpga_serial_transport.json", report)
    state["fpga_serial_transport"] = report
    return report

from typing import Any, Dict, List, Optional

from .contracts import ToolAdapter


DEFAULT_ADAPTERS: Dict[str, ToolAdapter] = {
    "verilator.lint": ToolAdapter(
        adapter_id="verilator.lint",
        capability="rtl_lint",
        tool="verilator",
        description="Lint synthesizable Verilog/SystemVerilog with Verilator.",
        command_template=["{tool}", "--lint-only", "{rtl_files}"],
        expected_outputs=["*.log"],
        log_patterns=["%Error", "%Warning"],
    ),
    "verilator.coverage": ToolAdapter(
        adapter_id="verilator.coverage",
        capability="code_coverage",
        tool="verilator_coverage",
        description="Collect line/branch/condition coverage from Verilator LCOV outputs.",
        command_template=["{tool}", "--write-info", "{lcov_out}", "{coverage_dat}"],
        expected_outputs=["coverage.info", "coverage.json"],
        log_patterns=["Total coverage"],
    ),
    "symbiyosys.prove": ToolAdapter(
        adapter_id="symbiyosys.prove",
        capability="formal",
        tool="sby",
        description="Run SymbiYosys proof tasks with the selected solver.",
        command_template=["{tool}", "-f", "{sby_file}"],
        expected_outputs=["*.sby", "status", "*.log"],
        log_patterns=["PASS", "FAIL", "UNKNOWN"],
    ),
    "yosys.synthesis": ToolAdapter(
        adapter_id="yosys.synthesis",
        capability="synthesis",
        tool="yosys",
        description="Run Yosys synthesis/readiness checks.",
        command_template=["{tool}", "-q", "-l", "{log_file}", "{script}"],
        expected_outputs=["*.v", "*.json", "*.log"],
        log_patterns=["ERROR", "Warning"],
    ),
    "gem5.sim": ToolAdapter(
        adapter_id="gem5.sim",
        capability="system_architecture_sim",
        tool="gem5_x86",
        description="Run gem5 simulation with architecture config script.",
        command_template=["{tool}", "-d", "{out_dir}", "{config_py}"],
        expected_outputs=["stats.txt", "config.json", "gem5_run_results.json"],
        log_patterns=["fatal:", "warn:"],
    ),
    "synopsys.dc.synthesis": ToolAdapter(
        adapter_id="synopsys.dc.synthesis",
        capability="synthesis",
        tool="synopsys_dc",
        description="Customer-private Synopsys Design Compiler synthesis adapter template.",
        command_template=["{tool}", "-f", "{script}"],
        expected_outputs=["*.ddc", "*.v", "*.rpt", "*.log"],
        log_patterns=["Error:", "Warning:"],
    ),
    "cadence.xcelium.sim": ToolAdapter(
        adapter_id="cadence.xcelium.sim",
        capability="simulation",
        tool="xcelium",
        description="Customer-private Cadence Xcelium simulation adapter template.",
        command_template=["{tool}", "-f", "{filelist}", "-top", "{top}"],
        expected_outputs=["xrun.log", "*.vcd", "*.ucd"],
        log_patterns=["*E,", "*W,"],
    ),
}


def list_adapters() -> List[Dict[str, Any]]:
    return [adapter.to_dict() for adapter in DEFAULT_ADAPTERS.values()]


def resolve_adapter(adapter_id: str) -> Optional[ToolAdapter]:
    return DEFAULT_ADAPTERS.get(adapter_id)

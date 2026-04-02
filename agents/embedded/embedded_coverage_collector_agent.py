import json
import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from ._embedded_common import ensure_workflow_dir, write_artifact

AGENT_NAME = "Embedded Coverage Collector Agent"
PHASE = "coverage"
OUTPUT_PATH = "firmware/validate/coverage.md"
OUTPUT_FW = "firmware/validate/coverage_fw.md"
OUTPUT_RTL = "firmware/validate/coverage_rtl.md"
SUMMARY_JSON = "firmware/validate/coverage_summary.json"


def _which(name: str) -> Optional[str]:
    return shutil.which(name)


def _tool_snapshot() -> Dict[str, Any]:
    return {
        "cargo": _which("cargo"),
        "llvm_cov": _which("llvm-cov"),
        "llvm_profdata": _which("llvm-profdata"),
        "gcov": _which("gcov"),
        "verilator": _which("verilator"),
    }


def _resolve_target_info(state: Dict[str, Any]) -> Dict[str, str]:
    toolchain = state.get("toolchain") or {}
    firmware_build = state.get("firmware_build") or {}
    return {
        "target_triple": str(toolchain.get("target_triple") or firmware_build.get("target_triple") or state.get("target_triple") or "<TARGET_TRIPLE>"),
        "bin_name": str(toolchain.get("bin_name") or firmware_build.get("bin_name") or state.get("firmware_bin_name") or "<BIN_NAME>"),
    }


def _fw_markdown(info: Dict[str, str], tools: Dict[str, Any]) -> str:
    cargo = bool(tools.get("cargo"))
    llvm = bool(tools.get("llvm_cov")) and bool(tools.get("llvm_profdata"))
    gcov = bool(tools.get("gcov"))

    assumptions = [
        "<!-- ASSUMPTION: Firmware coverage collection depends on the actual firmware language/toolchain used in this workflow. -->",
        "<!-- ASSUMPTION: Confirm compiler and target support before enabling instrumentation in CI. -->",
    ]
    lines: List[str] = assumptions + ["", "# Firmware Coverage Collection", ""]

    lines.extend([
        "## Method A: Rust coverage (cargo-llvm-cov)",
        f"- Available in environment: `{cargo and llvm}`",
        f"- Target triple: `{info['target_triple']}`",
        f"- Binary name: `{info['bin_name']}`",
        "- Example command template:",
        "```bash",
        "cargo llvm-cov --no-report --release --target <TARGET_TRIPLE>",
        "cargo llvm-cov report --html",
        "```",
        "- Expected report location: `target/llvm-cov/html/index.html`",
        "",
        "## Method B: GCC/gcov style coverage",
        f"- Available in environment: `{gcov}`",
        "- Example command template:",
        "```bash",
        "<CC> --coverage -o <BIN_NAME> <SOURCES>",
        "./<BIN_NAME>",
        "gcov <SOURCE_FILE>",
        "```",
        "- Expected report location: `*.gcov` beside instrumented sources or build directory.",
        "",
        "## Method C: Not supported in v1",
        "- Use this when the firmware toolchain is neither Rust nor GCC-compatible, or when target execution cannot emit host-readable coverage data.",
        "- Enable later by adding toolchain-specific instrumentation and export steps.",
        "",
        "## Recommendation",
        "- Pick the method that matches the actual firmware compiler in this workflow.",
        "- Do not fabricate coverage numbers if instrumentation was not enabled.",
        "",
    ])
    return "\n".join(lines)


def _rtl_markdown(tools: Dict[str, Any]) -> str:
    assumptions = [
        "<!-- ASSUMPTION: RTL coverage capability depends on simulator version and compile-time flags. -->",
        "<!-- ASSUMPTION: Confirm supported flags with `verilator --help` before enabling coverage in CI. -->",
    ]
    has_verilator = bool(tools.get("verilator"))
    lines: List[str] = assumptions + ["", "# RTL Coverage Collection", ""]
    lines.extend([
        f"- Verilator available in environment: `{has_verilator}`",
        "- Example command template:",
        "```bash",
        "verilator <VERILATOR_COVERAGE_FLAGS> --cc --build --trace -f firmware/validate/verilator_rtl_filelist.f",
        "./obj_dir/V<top_module>",
        "```",
        "- Common coverage flag examples to verify against your version:",
        "  - `--coverage`",
        "  - `--coverage-line`",
        "  - `--coverage-toggle`",
        "- Expected report location: simulator output directory, version-dependent.",
        "- If unsupported, report RTL coverage as unavailable rather than inventing percentages.",
        "",
    ])
    return "\n".join(lines)


def _summary_markdown(tools: Dict[str, Any]) -> str:
    notes: List[str] = []
    if tools.get("cargo") and tools.get("llvm_cov"):
        notes.append("Rust firmware coverage can be enabled with cargo-llvm-cov if the build target supports it.")
    else:
        notes.append("Rust firmware coverage tooling is not fully available in the current environment.")
    if tools.get("verilator"):
        notes.append("RTL coverage may be possible with Verilator, subject to version-specific flags.")
    else:
        notes.append("RTL coverage tooling is not available in the current environment.")

    lines = [
        "# Coverage Summary",
        "",
        "| FW line % | FW function % | RTL line % | RTL toggle % | Notes |",
        "|---|---:|---:|---:|---|",
        f"| unavailable | unavailable | unavailable | unavailable | {' '.join(notes)} |",
        "",
        "Coverage percentages remain unavailable until real instrumentation and execution data are present.",
        "",
    ]
    return "\n".join(lines)


def run_agent(state: dict) -> dict:
    print(f"\n🚀 Running {AGENT_NAME}...")
    ensure_workflow_dir(state)

    tools = _tool_snapshot()
    info = _resolve_target_info(state)

    coverage_md = _summary_markdown(tools)
    coverage_fw_md = _fw_markdown(info, tools)
    coverage_rtl_md = _rtl_markdown(tools)
    summary = {
        "agent": AGENT_NAME,
        "tool_snapshot": tools,
        "target_info": info,
        "coverage_available": False,
        "functional_coverage_pct": None,
        "rtl_coverage_pct": None,
        "assertion_coverage_pct": None,
    }

    write_artifact(state, OUTPUT_PATH, coverage_md, key=os.path.basename(OUTPUT_PATH))
    write_artifact(state, OUTPUT_FW, coverage_fw_md, key=os.path.basename(OUTPUT_FW))
    write_artifact(state, OUTPUT_RTL, coverage_rtl_md, key=os.path.basename(OUTPUT_RTL))
    write_artifact(state, SUMMARY_JSON, json.dumps(summary, indent=2), key=os.path.basename(SUMMARY_JSON))

    state["embedded_coverage"] = summary
    state["embedded_coverage_summary"] = summary
    state["embedded_coverage_summary_path"] = SUMMARY_JSON
    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH
    return state

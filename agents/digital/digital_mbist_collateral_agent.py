import glob
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from tooling.runner import tool_path
from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "Digital MBIST Collateral Agent"


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _write_text(path: str, content: str) -> None:
    _ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _read_text(path: str | None) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _existing_path(value: Any, workflow_dir: str | None = None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    candidates = [raw]
    if workflow_dir and not os.path.isabs(raw):
        candidates.insert(0, os.path.join(workflow_dir, raw))
    for cand in candidates:
        try:
            cand = os.path.abspath(cand)
            if os.path.exists(cand):
                return cand
        except (TypeError, ValueError, OSError):
            continue
    return None


def _rtl_files(state: dict, workflow_dir: str) -> list[str]:
    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    handoff = digital.get("handoff") if isinstance(digital.get("handoff"), dict) else {}
    candidates: list[str] = []
    for value in (
        state.get("rtl_files"),
        digital.get("rtl_files"),
        handoff.get("rtl_files"),
        handoff.get("imported_rtl_files"),
    ):
        if isinstance(value, list):
            candidates.extend(str(item) for item in value)
    paths = [_existing_path(item, workflow_dir) for item in candidates]
    found = [item for item in paths if item]
    if found:
        return sorted(dict.fromkeys(found))
    patterns = [
        os.path.join(workflow_dir, "handoff", "rtl", "*.sv"),
        os.path.join(workflow_dir, "handoff", "rtl", "*.v"),
        os.path.join(workflow_dir, "digital", "handoff", "rtl", "*.sv"),
        os.path.join(workflow_dir, "digital", "handoff", "rtl", "*.v"),
        os.path.join(workflow_dir, "rtl", "*.sv"),
        os.path.join(workflow_dir, "rtl", "*.v"),
    ]
    hits: list[str] = []
    for pattern in patterns:
        hits.extend(glob.glob(pattern))
    return sorted(dict.fromkeys(os.path.abspath(path) for path in hits))


def _synth_netlist(state: dict, workflow_dir: str) -> str | None:
    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    synth = digital.get("synth") if isinstance(digital.get("synth"), dict) else {}
    for cand in (synth.get("netlist"), digital.get("synth_netlist"), state.get("synth_netlist")):
        path = _existing_path(cand, workflow_dir)
        if path:
            return path
    hits = sorted(glob.glob(os.path.join(workflow_dir, "digital", "synth", "netlist", "*.v")))
    return os.path.abspath(hits[0]) if hits else None


def _top_module(state: dict, files: list[str]) -> str:
    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    for value in (digital.get("top_module"), state.get("top_module")):
        if isinstance(value, str) and value.strip() and value.strip() != "imported_from_arch2rtl":
            return value.strip()
    for path in files:
        match = re.search(r"^\s*module\s+([A-Za-z_][A-Za-z0-9_$]*)\b", _read_text(path), flags=re.MULTILINE)
        if match:
            return match.group(1)
    return "top"


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"//.*", "", text)


def _range_width(range_text: str | None) -> int:
    if not range_text:
        return 1
    match = re.search(r"\[\s*(\d+)\s*:\s*(\d+)\s*\]", range_text)
    if not match:
        return 1
    return abs(int(match.group(1)) - int(match.group(2))) + 1


def _detect_memories(files: list[str]) -> list[dict[str, Any]]:
    memories: list[dict[str, Any]] = []
    macro_pattern = re.compile(
        r"^\s*(?P<cell>[A-Za-z_][A-Za-z0-9_$]*(?:sram|ram|mem|rf)[A-Za-z0-9_$]*)\s+"
        r"(?P<inst>[A-Za-z_][A-Za-z0-9_$]*)\s*\(",
        flags=re.IGNORECASE | re.MULTILINE,
    )
    array_pattern = re.compile(
        r"\b(?:reg|logic)\s+(?P<width>\[[^\]]+\]\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_$]*)\s*"
        r"(?P<depth>\[[^\]]+\])",
        flags=re.IGNORECASE,
    )
    for path in files:
        text = _strip_comments(_read_text(path))
        for match in macro_pattern.finditer(text):
            memories.append({
                "name": match.group("inst"),
                "kind": "memory_macro_instance",
                "cell": match.group("cell"),
                "source": os.path.basename(path),
                "width_bits": None,
                "depth": None,
                "estimated_bits": None,
            })
        for match in array_pattern.finditer(text):
            name = match.group("name")
            if not re.search(r"(mem|ram|sram|fifo|buffer|table|cache|rf)", name, flags=re.IGNORECASE):
                continue
            depth_range = match.group("depth")
            depth_match = re.search(r"\[\s*(\d+)\s*:\s*(\d+)\s*\]", depth_range)
            depth = abs(int(depth_match.group(1)) - int(depth_match.group(2))) + 1 if depth_match else None
            width = _range_width(match.group("width"))
            memories.append({
                "name": name,
                "kind": "inferred_register_array",
                "cell": None,
                "source": os.path.basename(path),
                "width_bits": width,
                "depth": depth,
                "estimated_bits": width * depth if depth else None,
            })
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for item in memories:
        deduped[(str(item.get("source")), str(item.get("name")))] = item
    return list(deduped.values())


def _mbist_controller_rtl() -> str:
    return """// Auto-generated limited MBIST controller collateral.
// Demonstration-quality March C- controller for a single-port scratchpad.
module chiploop_mbist_controller #(
  parameter int ADDR_WIDTH = 4,
  parameter int DATA_WIDTH = 8
) (
  input  logic                  clk,
  input  logic                  reset_n,
  input  logic                  start,
  output logic                  done,
  output logic                  fail,
  output logic [ADDR_WIDTH-1:0] mem_addr,
  output logic                  mem_we,
  output logic [DATA_WIDTH-1:0] mem_wdata,
  input  logic [DATA_WIDTH-1:0] mem_rdata
);
  typedef enum logic [2:0] {
    IDLE,
    WRITE_ZERO_UP,
    READ_ZERO_WRITE_ONE_UP,
    READ_ONE_WRITE_ZERO_DOWN,
    READ_ZERO_DOWN,
    DONE
  } state_t;

  state_t state;
  logic [ADDR_WIDTH-1:0] addr;

  assign mem_addr = addr;

  always_ff @(posedge clk or negedge reset_n) begin
    if (!reset_n) begin
      state <= IDLE;
      addr <= '0;
      done <= 1'b0;
      fail <= 1'b0;
      mem_we <= 1'b0;
      mem_wdata <= '0;
    end else begin
      done <= 1'b0;
      mem_we <= 1'b0;
      unique case (state)
        IDLE: begin
          fail <= 1'b0;
          addr <= '0;
          if (start) state <= WRITE_ZERO_UP;
        end
        WRITE_ZERO_UP: begin
          mem_we <= 1'b1;
          mem_wdata <= '0;
          if (&addr) begin
            addr <= '0;
            state <= READ_ZERO_WRITE_ONE_UP;
          end else begin
            addr <= addr + 1'b1;
          end
        end
        READ_ZERO_WRITE_ONE_UP: begin
          if (mem_rdata != '0) fail <= 1'b1;
          mem_we <= 1'b1;
          mem_wdata <= '1;
          if (&addr) begin
            addr <= '1;
            state <= READ_ONE_WRITE_ZERO_DOWN;
          end else begin
            addr <= addr + 1'b1;
          end
        end
        READ_ONE_WRITE_ZERO_DOWN: begin
          if (mem_rdata != '1) fail <= 1'b1;
          mem_we <= 1'b1;
          mem_wdata <= '0;
          if (addr == '0) begin
            addr <= '1;
            state <= READ_ZERO_DOWN;
          end else begin
            addr <= addr - 1'b1;
          end
        end
        READ_ZERO_DOWN: begin
          if (mem_rdata != '0) fail <= 1'b1;
          if (addr == '0) state <= DONE;
          else addr <= addr - 1'b1;
        end
        DONE: begin
          done <= 1'b1;
          if (!start) state <= IDLE;
        end
        default: state <= IDLE;
      endcase
    end
  end
endmodule
"""


def _scratchpad_rtl() -> str:
    return """// Auto-generated demo scratchpad memory for MBIST collateral.
// This is not inserted into the source design unless a downstream integrator connects it.
module chiploop_mbist_demo_scratchpad #(
  parameter int ADDR_WIDTH = 4,
  parameter int DATA_WIDTH = 8
) (
  input  logic                  clk,
  input  logic                  we,
  input  logic [ADDR_WIDTH-1:0] addr,
  input  logic [DATA_WIDTH-1:0] wdata,
  output logic [DATA_WIDTH-1:0] rdata
);
  logic [DATA_WIDTH-1:0] mem [0:(1 << ADDR_WIDTH)-1];

  always_ff @(posedge clk) begin
    if (we) mem[addr] <= wdata;
    rdata <= mem[addr];
  end
endmodule
"""


def _testplan(memory_count: int) -> str:
    scope = "detected design memories" if memory_count else "demo scratchpad memory only"
    return f"""# Limited MBIST Test Plan

- Scope: {scope}
- Algorithm: March C-
- Fault classes demonstrated: stuck-at, transition-like read/write checks
- Open-source status: generated RTL collateral and simulation-ready intent only
- Production DFT note: MBIST insertion, repair, redundancy, fuse programming, and signoff coverage should use customer DFT tools through ChipLoop private adapters.

## Sequence

1. Write 0 ascending addresses.
2. Read 0 and write 1 ascending addresses.
3. Read 1 and write 0 descending addresses.
4. Read 0 descending addresses.
5. Report fail on first observed mismatch.
"""


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = os.path.abspath(state.get("workflow_dir") or f"backend/workflows/{workflow_id}")
    stage_dir = os.path.join(workflow_dir, "digital", "mbist")
    _ensure_dir(stage_dir)

    rtl_files = _rtl_files(state, workflow_dir)
    netlist = _synth_netlist(state, workflow_dir)
    scan_files = rtl_files + ([netlist] if netlist else [])
    top = _top_module(state, scan_files)
    memories = _detect_memories(scan_files)
    openram = tool_path("openram", state)
    autombist = tool_path("autombist", state)
    demo_requested = bool(state.get("demo_mbist") or state.get("mbist_demo"))
    generated_demo = not memories and demo_requested
    if memories and autombist:
        status = "autombist_adapter_ready"
    elif memories:
        status = "mbist_collateral_generated"
    elif generated_demo:
        status = "demo_collateral_no_design_memory"
    else:
        status = "not_applicable"
    memory_bits = sum(int(item.get("estimated_bits") or 0) for item in memories)

    intent = {
        "top_module": top,
        "algorithm": "march_c_minus",
        "scope": "detected_design_memories" if memories else "demo_scratchpad_reference" if generated_demo else "not_applicable",
        "memories": memories,
        "demo_scratchpad": {
            "generated": generated_demo,
            "addr_width": 4,
            "data_width": 8,
            "bits": 128,
        },
        "limitations": [
            "Generated collateral is limited open-source MBIST evidence, not production DFT signoff.",
            "No repair, redundancy, fuse programming, ATPG, or foundry-qualified memory compiler integration is performed.",
        ],
    }
    summary = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "status": status,
        "top_module": top,
        "memory_count": len(memories),
        "estimated_memory_bits": memory_bits,
        "algorithm": "March C-",
        "demo_scratchpad_generated": generated_demo,
        "openram_available": bool(openram),
        "openram_executable": openram,
        "autombist_available": bool(autombist),
        "autombist_executable": autombist,
        "open_source_support": "limited_rtl_collateral",
        "production_adapter_required_for": ["Tessent", "Synopsys TestMAX/DFT Compiler", "Cadence Modus"],
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "artifacts": {
            "summary": "digital/mbist/mbist_summary.json",
            "intent": "digital/mbist/mbist_intent.json",
            "controller": "digital/mbist/chiploop_mbist_controller.sv",
            "scratchpad": "digital/mbist/chiploop_mbist_demo_scratchpad.sv" if generated_demo else None,
            "testplan": "digital/mbist/mbist_march_c_testplan.md",
            "report": "digital/mbist/mbist_report.md",
        },
    }
    report = "\n".join([
        "# Limited MBIST Collateral",
        "",
        f"- Status: `{status}`",
        f"- Top module: `{top}`",
        f"- Detected memories: `{len(memories)}`",
        f"- Estimated memory bits: `{memory_bits}`",
        f"- Algorithm: `March C-`",
        f"- OpenRAM available: `{bool(openram)}`",
        f"- autombist available: `{bool(autombist)}`",
        f"- Demo scratchpad generated: `{generated_demo}`",
        "",
        "For PWM and other memoryless designs, ChipLoop does not claim MBIST was inserted into the design. MBIST is reported as not applicable unless design memories are detected or an explicit MBIST demo mode is requested.",
        "For production MBIST/LBIST, connect this agent to customer DFT tools through a private adapter.",
    ]) + "\n"

    files = {
        "mbist_summary.json": json.dumps(summary, indent=2),
        "mbist_intent.json": json.dumps(intent, indent=2),
        "chiploop_mbist_controller.sv": _mbist_controller_rtl(),
        "mbist_march_c_testplan.md": _testplan(len(memories)),
        "mbist_report.md": report,
    }
    if generated_demo:
        files["chiploop_mbist_demo_scratchpad.sv"] = _scratchpad_rtl()

    for name, content in files.items():
        _write_text(os.path.join(stage_dir, name), content)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", f"mbist/{name}", content)

    digital = state.setdefault("digital", {})
    digital["mbist"] = {
        "status": status,
        "summary_json": os.path.join(stage_dir, "mbist_summary.json"),
        "intent_json": os.path.join(stage_dir, "mbist_intent.json"),
        "controller_rtl": os.path.join(stage_dir, "chiploop_mbist_controller.sv"),
    }
    state["status"] = f"{AGENT_NAME}: {status}"
    return state

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Golden Model Comparison Agent"


def _now() -> str:
    return datetime.now().isoformat()


def _safe_read_json(path: Optional[str]) -> Dict[str, Any]:
    try:
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                obj = json.load(f)
                return obj if isinstance(obj, dict) else {}
    except Exception:
        pass
    return {}


def _write_file(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _record_text(workflow_id: str, subdir: str, filename: str, content: str) -> Optional[str]:
    try:
        return save_text_artifact_and_record(workflow_id, AGENT_NAME, subdir, filename, content)
    except Exception:
        return None


def _first_existing(paths: List[Optional[str]]) -> Optional[str]:
    for path in paths:
        if path and os.path.exists(path):
            return path
    return None


def _contract_path(workflow_dir: str, state: Dict[str, Any]) -> Optional[str]:
    return _first_existing(
        [
            state.get("tb_contract_json"),
            os.path.join(workflow_dir, "vv", "tb", "tb_contract.json"),
        ]
    )


def _ports_from_contract(contract: Dict[str, Any]) -> tuple[List[str], List[str], List[str]]:
    ports = contract.get("ports") if isinstance(contract.get("ports"), list) else []
    inputs: List[str] = []
    outputs: List[str] = []
    for port in ports:
        if not isinstance(port, dict) or not port.get("name"):
            continue
        direction = str(port.get("direction") or "").lower()
        name = str(port["name"])
        if direction in {"input", "inout"}:
            inputs.append(name)
        if direction == "output":
            outputs.append(name)
    clocks = contract.get("clock_names") if isinstance(contract.get("clock_names"), list) else []
    clocks = [str(c) for c in clocks if str(c).strip()]
    return inputs, outputs, clocks


def _gen_scoreboard(top: str, inputs: List[str], outputs: List[str], clocks: List[str]) -> str:
    clk = clocks[0] if clocks else None
    return f'''"""Auto-generated system-level scoreboard for {top}.

This is intentionally conservative: it records DUT observations and compares only
signals that a generated or user-authored golden model returns. The default
golden_model() returns an empty expectation set, so enabling golden model support
never invents pass/fail claims.
"""

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List

import cocotb
from cocotb.triggers import RisingEdge, Timer

TOP = {json.dumps(top)}
CLK = {json.dumps(clk)}
INPUT_PORTS = {json.dumps(inputs, indent=2)}
OUTPUT_PORTS = {json.dumps(outputs, indent=2)}


@dataclass
class Txn:
    inputs: Dict[str, int]
    outputs: Dict[str, int]
    time_ns: int


def golden_model(txn: Txn) -> Dict[str, int]:
    """Return expected output values keyed by output port name."""
    return {{}}


class Scoreboard:
    def __init__(self, dut):
        self.dut = dut
        self.enabled = False
        self.samples = 0
        self.mismatches: List[Dict[str, Any]] = []

    def start(self):
        self.enabled = True
        cocotb.start_soon(self._run())

    def stop(self):
        self.enabled = False
        self._write_report()

    async def _run(self):
        while self.enabled:
            if CLK and hasattr(self.dut, CLK):
                await RisingEdge(getattr(self.dut, CLK))
            else:
                await Timer(10, units="ns")
            self.observe()

    def _read_int(self, name: str):
        try:
            return int(getattr(self.dut, name).value)
        except Exception:
            return None

    def _snapshot(self) -> Txn:
        inputs = {{name: value for name in INPUT_PORTS if (value := self._read_int(name)) is not None}}
        outputs = {{name: value for name in OUTPUT_PORTS if (value := self._read_int(name)) is not None}}
        return Txn(inputs=inputs, outputs=outputs, time_ns=0)

    def observe(self):
        if not self.enabled:
            return
        txn = self._snapshot()
        self.samples += 1
        expected = golden_model(txn)
        if not isinstance(expected, dict):
            return
        for name, exp in expected.items():
            obs = txn.outputs.get(name)
            if obs is None:
                continue
            try:
                exp_i = int(exp)
                obs_i = int(obs)
            except Exception:
                continue
            if exp_i != obs_i:
                self.mismatches.append({{
                    "signal": name,
                    "expected": exp_i,
                    "observed": obs_i,
                    "inputs": txn.inputs,
                }})

    def _write_report(self):
        reports_dir = os.path.join(os.path.dirname(__file__), "reports")
        os.makedirs(reports_dir, exist_ok=True)
        report = {{
            "type": "system_golden_model_scoreboard",
            "top_module": TOP,
            "status": "pass" if not self.mismatches else "fail",
            "samples": self.samples,
            "mismatch_count": len(self.mismatches),
            "mismatches": self.mismatches[:200],
            "model": "chiploop_python_scoreboard",
        }}
        with open(os.path.join(reports_dir, "scoreboard_report.json"), "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
'''


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    workflow_id = str(state.get("workflow_id") or "default")
    workflow_dir = str(state.get("workflow_dir") or f"backend/workflows/{workflow_id}")
    tb_dir = os.path.join(workflow_dir, "vv", "tb")
    reports_dir = os.path.join(tb_dir, "reports")
    os.makedirs(tb_dir, exist_ok=True)
    os.makedirs(reports_dir, exist_ok=True)

    contract = _safe_read_json(_contract_path(workflow_dir, state))
    top = str(
        state.get("soc_top_sim_module")
        or state.get("top_module")
        or contract.get("top_module")
        or "soc_top_sim"
    )
    inputs, outputs, clocks = _ports_from_contract(contract)
    scoreboard_py = _gen_scoreboard(top, inputs, outputs, clocks)
    _write_file(os.path.join(tb_dir, "scoreboard.py"), scoreboard_py)

    report = {
        "type": "system_golden_model_generation",
        "version": "1.0",
        "generated_at": _now(),
        "top_module": top,
        "model": "chiploop_python_scoreboard",
        "status": "generated",
        "input_ports": inputs,
        "output_ports": outputs,
        "clock_names": clocks,
        "contract_json": str(_contract_path(workflow_dir, state) or ""),
        "artifacts": {},
    }
    report["artifacts"]["scoreboard_py"] = _record_text(workflow_id, "vv/tb", "scoreboard.py", scoreboard_py)
    report_text = json.dumps(report, indent=2)
    _write_file(os.path.join(tb_dir, "golden_model_generation_report.json"), report_text)
    report["artifacts"]["report"] = _record_text(
        workflow_id,
        "vv/tb",
        "golden_model_generation_report.json",
        json.dumps(report, indent=2),
    )

    state.setdefault("system_sim", {})
    state["system_golden_model_report_json"] = os.path.join(tb_dir, "golden_model_generation_report.json")
    state["system_sim"]["golden_model"] = report
    state.setdefault("vv", {})
    state["vv"]["golden_model"] = report
    return state

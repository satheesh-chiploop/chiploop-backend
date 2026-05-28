import json
from pathlib import Path
from typing import Any, Dict, List

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "Digital Failure Triage Agent"


def _read_tail(path: Path, limit: int = 120) -> List[str]:
    try:
        return path.read_text(encoding="utf-8", errors="ignore").splitlines()[-limit:]
    except Exception:
        return []


def _classify(result: Dict[str, Any], stdout: List[str], stderr: List[str]) -> Dict[str, Any]:
    text = "\n".join(stdout + stderr).lower()
    if "assert" in text or "sva" in text:
        kind = "rtl_or_assertion_failure"
        recommendation = "Rerun this testcase/seed with waveform enabled and inspect the assertion firing cycle."
    elif "modulenotfound" in text or "importerror" in text or "make:" in text:
        kind = "environment_or_testbench_failure"
        recommendation = "Check cocotb/python environment, generated Makefile, and handoff paths before changing RTL."
    elif "scoreboard" in text or "mismatch" in text or "expected" in text:
        kind = "rtl_or_golden_model_mismatch"
        recommendation = "Compare DUT output against the expected transaction and confirm whether spec or RTL is wrong."
    elif int(result.get("rc") or 0) != 0:
        kind = "simulation_failure"
        recommendation = "Replay the failing seed with waveform and full logs."
    else:
        kind = "unknown"
        recommendation = "Review logs and rerun with debug waveform enabled."
    return {"classification": kind, "recommendation": recommendation}


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    workflow_id = str(state.get("workflow_id") or "default")
    workflow_dir = Path(str(state.get("workflow_dir") or f"backend/workflows/{workflow_id}"))
    source_dir = Path(str(state.get("source_verify_workflow_dir") or ""))
    out_dir = workflow_dir / "verify_closure"
    out_dir.mkdir(parents=True, exist_ok=True)

    sim = state.get("source_simulation_execution_summary") if isinstance(state.get("source_simulation_execution_summary"), dict) else {}
    results = sim.get("results") if isinstance(sim.get("results"), list) else []
    failed = [r for r in results if isinstance(r, dict) and not r.get("pass")]
    log_dir = source_dir / "vv" / "tb" / "reports" / "run_logs"

    triage = []
    for result in failed:
        testcase = str(result.get("testcase") or "unknown")
        seed = str(result.get("seed") or "unknown")
        stdout = _read_tail(log_dir / f"{testcase}__seed_{seed}.stdout.log")
        stderr = _read_tail(log_dir / f"{testcase}__seed_{seed}.stderr.log")
        classified = _classify(result, stdout, stderr)
        triage.append({
            "testcase": testcase,
            "seed": seed,
            "returncode": result.get("rc"),
            "classification": classified["classification"],
            "recommendation": classified["recommendation"],
            "debug_replay": {
                "enable_waveform": True,
                "rerun_scope": "single_testcase_seed",
                "testcase": testcase,
                "seed": seed,
            },
            "stdout_tail": stdout[-20:],
            "stderr_tail": stderr[-20:],
        })

    report = {
        "type": "failure_triage",
        "total_failures": len(failed),
        "failures": triage,
        "needs_debug_replay": bool(triage),
    }
    txt = json.dumps(report, indent=2)
    md = "\n".join([
        "# Failure Triage",
        "",
        f"- Failing testcase/seed pairs: {len(triage)}",
        *[
            f"- {item['testcase']} seed {item['seed']}: {item['classification']} - {item['recommendation']}"
            for item in triage
        ],
    ]) + "\n"
    (out_dir / "failure_triage.json").write_text(txt, encoding="utf-8")
    (out_dir / "failure_triage.md").write_text(md, encoding="utf-8")
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "verify_closure", "failure_triage.json", txt)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "verify_closure", "failure_triage.md", md)
    state["failure_triage"] = report
    return state

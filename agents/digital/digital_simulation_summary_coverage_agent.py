import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, Optional

from utils.artifact_utils import save_text_artifact_and_record

logger = logging.getLogger("chiploop")


def _now() -> str:
    return datetime.now().isoformat()


def _log(path: str, msg: str, level: str = "info") -> None:
    if level == "error":
        logger.error(msg)
    elif level == "warning":
        logger.warning(msg)
    else:
        logger.info(msg)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"[{_now()}] [{level.upper()}] {msg}\n")


def _log_kv(path: str, key: str, value: Any) -> None:
    try:
        rendered = json.dumps(value, indent=2, default=str)
    except Exception:
        rendered = str(value)
    _log(path, f"{key}={rendered}")


def _safe_read_json(path: Optional[str]) -> Dict[str, Any]:
    try:
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                obj = json.load(f)
                if isinstance(obj, dict):
                    return obj
    except Exception:
        pass
    return {}


def _write_file(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _record_text(
    workflow_id: str,
    agent_name: str,
    subdir: str,
    filename: str,
    content: str,
):
    try:
        return save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir=subdir,
            filename=filename,
            content=content,
        )
    except Exception:
        return None


def run_agent(state: dict) -> dict:
    agent_name = "Digital Simulation Summary Coverage Agent"

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir") or f"backend/workflows/{workflow_id}"
    reports_dir = os.path.join(workflow_dir, "vv", "tb", "reports")
    os.makedirs(reports_dir, exist_ok=True)

    log_path = os.path.join("artifact", "simulation_summary_coverage_agent.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("Digital Simulation Summary Coverage Agent Log\n")

    _log(log_path, f"Starting {agent_name}...")

    try:
        sim_path = state.get("simulation_execution_summary_json") or os.path.join(
            reports_dir, "simulation_execution_summary.json"
        )
        cov_path = state.get("functional_coverage_summary_json") or os.path.join(
            reports_dir, "functional_coverage_summary.json"
        )

        _log(log_path, f"simulation_execution_summary_json={sim_path}")
        _log(log_path, f"functional_coverage_summary_json={cov_path}")

        sim_exists = bool(sim_path and os.path.exists(sim_path))
        cov_exists = bool(cov_path and os.path.exists(cov_path))

        sim = _safe_read_json(sim_path)
        cov = _safe_read_json(cov_path)

        sim_loaded = bool(sim)
        cov_loaded = bool(cov)

        if not cov_exists:
            coverage_status = "missing"
        elif cov_exists and not cov_loaded:
            coverage_status = "invalid"
        else:
            coverage_status = "ok"

        _log_kv(log_path, "sim_exists", sim_exists)
        _log_kv(log_path, "cov_exists", cov_exists)
        _log_kv(log_path, "sim_loaded", sim_loaded)
        _log_kv(log_path, "cov_loaded", cov_loaded)
        _log_kv(log_path, "coverage_status", coverage_status)

        summary = {
            "type": "simulation_summary_coverage",
            "simulation": {
                "total": sim.get("total"),
                "pass": sim.get("pass"),
                "fail": sim.get("fail"),
            },
            "coverage": {
                "status": coverage_status,
                "functional_coverage_pct": cov.get("functional_coverage_pct"),
                "bins_hit": cov.get("bins_hit"),
                "total_bins": cov.get("total_bins"),
            }
        }

        

        summary_txt = json.dumps(summary, indent=2)

        md_lines = [
            "# Simulation Summary + Coverage",
            "",
            f"- Total simulation runs: {summary['simulation']['total']}",
            f"- Simulation pass count: {summary['simulation']['pass']}",
            f"- Simulation fail count: {summary['simulation']['fail']}",
            f"- Coverage status: {summary['coverage']['status']}",
            f"- Functional coverage %: {summary['coverage']['functional_coverage_pct']}",
            f"- Coverage bins hit: {summary['coverage']['bins_hit']}",
            f"- Coverage total bins: {summary['coverage']['total_bins']}",
        ]

        
        md_txt = "\n".join(md_lines) + "\n"

        summary_path = os.path.join(reports_dir, "simulation_summary_coverage.json")
        md_path = os.path.join(reports_dir, "SIM_SUMMARY_COVERAGE.md")
        _write_file(summary_path, summary_txt)
        _write_file(md_path, md_txt)

        artifacts = {}
        artifacts["simulation_summary_coverage"] = _record_text(
            workflow_id, agent_name, "vv/tb/reports", "simulation_summary_coverage.json", summary_txt
        )
        artifacts["simulation_summary_coverage_md"] = _record_text(
            workflow_id, agent_name, "vv/tb/reports", "SIM_SUMMARY_COVERAGE.md", md_txt
        )

        report = {
            "type": "simulation_summary_coverage_report",
            "simulation_execution_summary_json": sim_path,
            "functional_coverage_summary_json": cov_path,
            "artifacts": artifacts,
        }
        rep_txt = json.dumps(report, indent=2)
        report_path = os.path.join(reports_dir, "simulation_summary_coverage_report.json")
        _write_file(report_path, rep_txt)

        artifacts["report"] = _record_text(
            workflow_id, agent_name, "vv/tb/reports", "simulation_summary_coverage_report.json", rep_txt
        )

        try:
            with open(log_path, "r", encoding="utf-8") as f:
                log_text = f.read()
        except Exception:
            log_text = ""
        artifacts["log"] = _record_text(
            workflow_id, agent_name, "vv", "simulation_summary_coverage_agent.log", log_text
        )

        _log(log_path, f"Final summary written: {summary_path}")

        state["final_summary"] = summary_path
        state["simulation_summary_coverage_json"] = summary_path
        state["simulation_summary_coverage_report_json"] = report_path
        state.setdefault("vv", {})
        state["vv"]["simulation_summary_coverage"] = report

        _log(log_path, f"{agent_name} completed successfully.")
        return state

    except Exception as e:
        _log(log_path, f"{agent_name} failed: {e}", level="error")
        raise
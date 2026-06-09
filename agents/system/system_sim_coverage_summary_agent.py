
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


def _first_existing(candidates: list[Optional[str]]) -> Optional[str]:
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None

def _write_file(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

def _record_text(workflow_id: str, agent_name: str, subdir: str, filename: str, content: str):
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
    agent_name = "System Simulation Coverage Summary Agent"

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir") or f"backend/workflows/{workflow_id}"
    reports_dir = os.path.join(workflow_dir, "vv", "tb", "reports")
    system_sim_dir = os.path.join(workflow_dir, "system", "sim")
    os.makedirs(reports_dir, exist_ok=True)
    os.makedirs(system_sim_dir, exist_ok=True)

    log_path = os.path.join("artifact", "system_simulation_summary_coverage_agent.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("System Simulation Coverage Summary Agent Log\n")

    _log(log_path, f"Starting {agent_name}...")

    try:
        sim_path = (
            state.get("simulation_execution_summary_json")
            or state.get("system_sim_execution_json")
            or os.path.join(system_sim_dir, "system_sim_execution.json")
        )
        cov_path = _first_existing([
            state.get("functional_coverage_summary_json"),
            state.get("system_functional_coverage_summary_json"),
            os.path.join(reports_dir, "functional_coverage_summary.json"),
            os.path.join(workflow_dir, "vv", "tb", "functional_coverage_summary.json"),
            os.path.join(workflow_dir, "vv", "tb", "coverage_generation_report.json"),
            os.path.join(workflow_dir, "vv", "tb", "coverage_spec.json"),
        ]) or os.path.join(reports_dir, "functional_coverage_summary.json")
        code_cov_path = _first_existing([
            state.get("code_coverage_summary_json"),
            os.path.join(reports_dir, "code_coverage_summary.json"),
            os.path.join(workflow_dir, "vv", "tb", "code_coverage_summary.json"),
        ]) or os.path.join(reports_dir, "code_coverage_summary.json")
        formal_path = _first_existing([
            state.get("system_formal_report_json"),
            state.get("formal_report_json"),
            os.path.join(workflow_dir, "vv", "formal", "formal_report.json"),
        ])

        _log(log_path, f"simulation_execution_summary_json={sim_path}")
        _log(log_path, f"functional_coverage_summary_json={cov_path}")
        _log(log_path, f"code_coverage_summary_json={code_cov_path}")
        _log(log_path, f"formal_report_json={formal_path}")

        sim_exists = bool(sim_path and os.path.exists(sim_path))
        cov_exists = bool(cov_path and os.path.exists(cov_path))
        code_cov_exists = bool(code_cov_path and os.path.exists(code_cov_path))
        formal_exists = bool(formal_path and os.path.exists(formal_path))

        sim = _safe_read_json(sim_path)
        cov = _safe_read_json(cov_path)
        code_cov = _safe_read_json(code_cov_path)
        formal = _safe_read_json(formal_path)

        sim_loaded = bool(sim)
        cov_loaded = bool(cov)
        code_cov_loaded = bool(code_cov)
        formal_loaded = bool(formal)

        if not cov_exists:
            coverage_status = "missing"
        elif cov_exists and not cov_loaded:
            coverage_status = "invalid"
        else:
            coverage_status = "ok"
        if not code_cov_exists:
            code_coverage_status = "missing"
        elif code_cov_exists and not code_cov_loaded:
            code_coverage_status = "invalid"
        else:
            code_coverage_status = str(code_cov.get("status") or "ok")
        formal_run = formal.get("run") if isinstance(formal.get("run"), dict) else {}
        if not formal_loaded:
            formal_status = "not_enabled"
        elif formal_run.get("attempted") is True:
            formal_status = "pass" if formal_run.get("returncode") == 0 else "fail"
        elif formal_run.get("disabled") is True:
            formal_status = "not_enabled"
        elif formal_run.get("available") is False:
            formal_status = "tool_unavailable"
        else:
            formal_status = "generated"

        functional_pct = cov.get("functional_coverage_pct")
        bins_hit = cov.get("bins_hit")
        total_bins = cov.get("total_bins")
        if functional_pct is None and isinstance(cov.get("coverage"), dict):
            c = cov.get("coverage") or {}
            functional_pct = c.get("pct") or c.get("coverage_pct") or c.get("percent")
            bins_hit = bins_hit if bins_hit is not None else c.get("bins_hit") or c.get("hit")
            total_bins = total_bins if total_bins is not None else c.get("total_bins") or c.get("total")

        summary = {
            "type": "system_simulation_summary_coverage",
            "simulation": {
                "status": sim.get("status"),
                "top_module": sim.get("top_module"),
                "total": sim.get("tests_run") if sim.get("tests_run") is not None else sim.get("total"),
                "pass": sim.get("tests_passed") if sim.get("tests_passed") is not None else sim.get("pass"),
                "fail": sim.get("tests_failed") if sim.get("tests_failed") is not None else sim.get("fail"),
                "total_runtime_sec": sim.get("total_runtime_sec"),
                "assertions_total": sim.get("assertions_total"),
                "assertion_failures_total": sim.get("assertion_failures_total"),
            },
            "coverage": {
                "status": coverage_status,
                "functional_coverage_pct": functional_pct,
                "bins_hit": bins_hit,
                "total_bins": total_bins,
                "functional": {
                    "status": coverage_status,
                    "coverage_pct": functional_pct,
                    "bins_hit": bins_hit,
                    "total_bins": total_bins,
                },
                "code": {
                    "status": code_coverage_status,
                    "line_coverage_pct": code_cov.get("line_coverage_pct"),
                    "line_hit": code_cov.get("line_hit"),
                    "line_found": code_cov.get("line_found"),
                    "branch_coverage_pct": code_cov.get("branch_coverage_pct"),
                    "branch_hit": code_cov.get("branch_hit"),
                    "branch_found": code_cov.get("branch_found"),
                    "condition_coverage_pct": code_cov.get("condition_coverage_pct"),
                    "condition_hit": code_cov.get("condition_hit"),
                    "condition_found": code_cov.get("condition_found"),
                    "condition_source": code_cov.get("condition_source") or "unavailable",
                    "toggle_coverage_pct": code_cov.get("toggle_coverage_pct"),
                    "toggle_hit": code_cov.get("toggle_hit"),
                    "toggle_found": code_cov.get("toggle_found"),
                    "toggle_source": code_cov.get("toggle_source") or "not_reported_by_verilator_lcov",
                    "missed_toggle_points": code_cov.get("missed_toggle_points") or [],
                    "tool": code_cov.get("tool") or "verilator_coverage",
                },
                "assertions": {
                    "status": "missing" if not sim.get("assertions_total") else ("failed" if sim.get("assertion_failures_total") else "ok"),
                    "assertions_generated": sim.get("assertions_total"),
                    "assertion_failures": sim.get("assertion_failures_total"),
                    "assertion_pass_pct": None if not sim.get("assertions_total") else round(
                        100.0 * max(float(sim.get("assertions_total") or 0) - float(sim.get("assertion_failures_total") or 0), 0.0) / float(sim.get("assertions_total") or 1),
                        3,
                    ),
                },
                "source_json": cov_path if cov_exists else None,
            },
            "formal": {
                "status": formal_status,
                "available": formal_run.get("available"),
                "attempted": formal_run.get("attempted"),
                "returncode": formal_run.get("returncode"),
                "blocked_reason": formal_run.get("blocked_reason"),
            },
            "golden_model": {"status": "not_enabled"},
            "toolchain": {
                "simulator": ((sim.get("toolchain") or {}).get("simulator") if isinstance(sim.get("toolchain"), dict) else None) or sim.get("simulator") or "verilator",
                "code_coverage": code_cov.get("tool") or ((sim.get("toolchain") or {}).get("code_coverage") if isinstance(sim.get("toolchain"), dict) else None) or "verilator_coverage",
                "formal": ((formal.get("toolchain") or {}).get("formal") if isinstance(formal.get("toolchain"), dict) else None) or ("symbiyosys" if formal_status not in {"not_enabled"} else "none"),
                "formal_solver": ((formal.get("toolchain") or {}).get("formal_solver") if isinstance(formal.get("toolchain"), dict) else None),
                "golden_model": "none",
            },
            "waveforms": sim.get("waveforms") or [],
        }

        _log_kv(log_path, "sim_exists", sim_exists)
        _log_kv(log_path, "cov_exists", cov_exists)
        _log_kv(log_path, "code_cov_exists", code_cov_exists)
        _log_kv(log_path, "formal_exists", formal_exists)
        _log_kv(log_path, "sim_loaded", sim_loaded)
        _log_kv(log_path, "cov_loaded", cov_loaded)
        _log_kv(log_path, "code_cov_loaded", code_cov_loaded)
        _log_kv(log_path, "formal_loaded", formal_loaded)
        _log_kv(log_path, "coverage_status", coverage_status)
        _log_kv(log_path, "code_coverage_status", code_coverage_status)
        _log_kv(log_path, "formal_status", formal_status)

        summary_txt = json.dumps(summary, indent=2)

        md_lines = [
            "# System Simulation Summary + Coverage",
            "",
            f"- Top module: {summary['simulation']['top_module']}",
            f"- Status: {summary['simulation']['status']}",
            f"- Total simulation runs: {summary['simulation']['total']}",
            f"- Simulation pass count: {summary['simulation']['pass']}",
            f"- Simulation fail count: {summary['simulation']['fail']}",
            f"- Total runtime (s): {summary['simulation']['total_runtime_sec']}",
            f"- Coverage status: {summary['coverage']['status']}",
            f"- Functional coverage %: {summary['coverage']['functional_coverage_pct']}",
            f"- Coverage bins hit: {summary['coverage']['bins_hit']}",
            f"- Coverage total bins: {summary['coverage']['total_bins']}",
            f"- Code line coverage %: {summary['coverage']['code']['line_coverage_pct']}",
            f"- Code branch coverage %: {summary['coverage']['code']['branch_coverage_pct']}",
            f"- Code condition coverage %: {summary['coverage']['code']['condition_coverage_pct']}",
            f"- Code toggle coverage %: {summary['coverage']['code']['toggle_coverage_pct']}",
            f"- Assertions total: {summary['simulation']['assertions_total']}",
            f"- Assertion failures: {summary['simulation']['assertion_failures_total']}",
            f"- Formal status: {summary['formal']['status']}",
        ]
        if summary["waveforms"]:
            md_lines.extend(["", "## Waveforms"])
            for w in summary["waveforms"]:
                md_lines.append(f"- `{w}`")
        md_txt = "\n".join(md_lines) + "\n"

        summary_path = os.path.join(system_sim_dir, "system_sim_dashboard.json")
        md_path = os.path.join(system_sim_dir, "system_sim_dashboard.md")
        _write_file(summary_path, summary_txt)
        _write_file(md_path, md_txt)

        artifacts = {}
        artifacts["system_sim_dashboard"] = _record_text(
            workflow_id, agent_name, "system/sim", "system_sim_dashboard.json", summary_txt
        )
        artifacts["system_sim_dashboard_md"] = _record_text(
            workflow_id, agent_name, "system/sim", "system_sim_dashboard.md", md_txt
        )

        report = {
            "type": "system_simulation_summary_coverage_report",
            "simulation_execution_summary_json": sim_path,
            "functional_coverage_summary_json": cov_path,
            "code_coverage_summary_json": code_cov_path,
            "formal_report_json": formal_path,
            "artifacts": artifacts,
        }
        rep_txt = json.dumps(report, indent=2)
        report_path = os.path.join(system_sim_dir, "system_sim_dashboard_report.json")
        _write_file(report_path, rep_txt)

        artifacts["report"] = _record_text(
            workflow_id, agent_name, "system/sim", "system_sim_dashboard_report.json", rep_txt
        )

        try:
            with open(log_path, "r", encoding="utf-8") as f:
                log_text = f.read()
        except Exception:
            log_text = ""
        artifacts["log"] = _record_text(
            workflow_id, agent_name, "vv", "system_simulation_summary_coverage_agent.log", log_text
        )

        _log(log_path, f"Final summary written: {summary_path}")

        state["final_summary"] = summary_path
        state["simulation_summary_coverage_json"] = summary_path
        state["system_sim_dashboard_json"] = summary_path
        state["system_sim_dashboard_report_json"] = report_path
        state.setdefault("system_sim", {})
        state["system_sim"]["dashboard"] = summary
        state["system_sim_dashboard"] = summary

        _log(log_path, f"{agent_name} completed successfully.")
        return state

    except Exception as e:
        _log(log_path, f"{agent_name} failed: {e}", level="error")
        raise

import json
import logging
import os
import re
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


def _find_first_json(workflow_dir: str, filename: str) -> Dict[str, Any]:
    for root, _, files in os.walk(workflow_dir):
        if filename in files:
            found = _safe_read_json(os.path.join(root, filename))
            if found:
                return found
    return {}


def _count_sva_assertions(state: Dict[str, Any], workflow_dir: str) -> int:
    sva = ((state.get("vv") or {}).get("sva") or {}) if isinstance(state.get("vv"), dict) else {}
    generated = sva.get("assertion_count")
    if isinstance(generated, int):
        return generated

    path = state.get("sva_assertions_path")
    candidates = [path] if isinstance(path, str) else []
    for root, _, files in os.walk(workflow_dir):
        for name in files:
            if name.endswith(".sv") and ("sva" in name.lower() or "assert" in name.lower()):
                candidates.append(os.path.join(root, name))

    for candidate in candidates:
        try:
            if candidate and os.path.exists(candidate):
                with open(candidate, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read()
                return len(re.findall(r"\bassert\s+property\b", text))
        except Exception:
            continue
    return 0


def _scan_assertion_failures(reports_dir: str) -> int:
    count = 0
    run_logs_dir = os.path.join(reports_dir, "run_logs")
    if not os.path.isdir(run_logs_dir):
        return 0
    patterns = (
        "assertion failed",
        "assert failed",
        "sva error",
        "%error",
    )
    for root, _, files in os.walk(run_logs_dir):
        for name in files:
            if not name.endswith(".log"):
                continue
            try:
                with open(os.path.join(root, name), "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read().lower()
                count += sum(text.count(pattern) for pattern in patterns)
            except Exception:
                continue
    return count


def _pct(hit: int, total: int) -> Optional[float]:
    if total <= 0:
        return None
    return round(100.0 * hit / total, 2)


def _status_label(value: Optional[float], status: str) -> str:
    if isinstance(value, (int, float)):
        return f"{value}%"
    return status or "Unavailable"

def _functional_gaps(cov: Dict[str, Any]) -> list[Dict[str, Any]]:
    gaps: list[Dict[str, Any]] = []
    for group in ("outputs", "inputs"):
        entries = cov.get(group) if isinstance(cov.get(group), dict) else {}
        for name, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            hit = int(entry.get("hit_bins") or 0)
            total = int(entry.get("total_bins") or 0)
            if total <= 0 or hit >= total:
                continue
            seen_values = entry.get("seen_values") if isinstance(entry.get("seen_values"), list) else []
            seen_zero = any(value == 0 for value in seen_values)
            seen_nonzero = any(isinstance(value, (int, float)) and value != 0 for value in seen_values)
            missing_bins = []
            if not seen_zero:
                missing_bins.append("zero")
            if not seen_nonzero:
                missing_bins.append("nonzero")
            gaps.append({
                "type": "functional_bin_gap",
                "signal": name,
                "group": group,
                "coverage_point": f"{group}.{name}",
                "hit_bins": hit,
                "total_bins": total,
                "seen_values": seen_values[:20],
                "missing_bins": missing_bins,
            })
    return gaps


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
        code_cov_path = state.get("code_coverage_summary_json") or os.path.join(
            reports_dir, "code_coverage_summary.json"
        )

        _log(log_path, f"simulation_execution_summary_json={sim_path}")
        _log(log_path, f"functional_coverage_summary_json={cov_path}")
        _log(log_path, f"code_coverage_summary_json={code_cov_path}")

        sim_exists = bool(sim_path and os.path.exists(sim_path))
        cov_exists = bool(cov_path and os.path.exists(cov_path))
        code_cov_exists = bool(code_cov_path and os.path.exists(code_cov_path))

        sim = _safe_read_json(sim_path)
        cov = _safe_read_json(cov_path)
        code_cov = _safe_read_json(code_cov_path)

        sim_loaded = bool(sim)
        cov_loaded = bool(cov)
        code_cov_loaded = bool(code_cov)

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

        assertion_count = _count_sva_assertions(state, workflow_dir)
        assertion_failures = _scan_assertion_failures(reports_dir)
        assertion_pass_pct = _pct(max(assertion_count - assertion_failures, 0), assertion_count)
        assertion_status = "missing" if assertion_count <= 0 else ("failed" if assertion_failures else "ok")

        formal = ((state.get("vv") or {}).get("formal") or {}) if isinstance(state.get("vv"), dict) else {}
        if not formal:
            formal = _find_first_json(workflow_dir, "formal_report.json")
        formal_run = formal.get("run") if isinstance(formal.get("run"), dict) else {}
        formal_status = "not_enabled"
        if formal:
            if formal_run.get("attempted") is True:
                formal_status = "pass" if formal_run.get("returncode") == 0 else "fail"
            elif formal_run.get("available") is False:
                formal_status = "tool_unavailable"
            else:
                formal_status = "generated"

        golden = ((state.get("vv") or {}).get("golden_model") or {}) if isinstance(state.get("vv"), dict) else {}
        if not golden:
            golden = _find_first_json(workflow_dir, "golden_model_generation_report.json")
        golden_status = "generated" if golden else "not_enabled"
        sim_toolchain = sim.get("toolchain") if isinstance(sim.get("toolchain"), dict) else {}
        formal_toolchain = formal.get("toolchain") if isinstance(formal.get("toolchain"), dict) else {}

        _log_kv(log_path, "sim_exists", sim_exists)
        _log_kv(log_path, "cov_exists", cov_exists)
        _log_kv(log_path, "code_cov_exists", code_cov_exists)
        _log_kv(log_path, "sim_loaded", sim_loaded)
        _log_kv(log_path, "cov_loaded", cov_loaded)
        _log_kv(log_path, "code_cov_loaded", code_cov_loaded)
        _log_kv(log_path, "coverage_status", coverage_status)
        _log_kv(log_path, "code_coverage_status", code_coverage_status)
        functional_gaps = _functional_gaps(cov)

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
                "functional": {
                    "status": coverage_status,
                    "coverage_pct": cov.get("functional_coverage_pct"),
                    "bins_hit": cov.get("bins_hit"),
                    "total_bins": cov.get("total_bins"),
                    "gap_count": len(functional_gaps),
                    "gaps": functional_gaps,
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
                    "tool": code_cov.get("tool") or "verilator_coverage",
                },
                "assertions": {
                    "status": assertion_status,
                    "assertions_generated": assertion_count,
                    "assertion_failures": assertion_failures,
                    "assertion_pass_pct": assertion_pass_pct,
                },
            },
            "formal": {
                "status": formal_status,
                "available": formal_run.get("available"),
                "attempted": formal_run.get("attempted"),
                "returncode": formal_run.get("returncode"),
            },
            "golden_model": {
                "status": golden_status,
                "top_module": golden.get("top_module") if golden else None,
            },
            "toolchain": {
                "simulator": sim_toolchain.get("simulator") or "verilator",
                "code_coverage": sim_toolchain.get("code_coverage") or code_cov.get("tool") or "verilator_coverage",
                "formal": formal_toolchain.get("formal") or ("symbiyosys" if formal_status != "not_enabled" else "none"),
                "formal_solver": formal_toolchain.get("formal_solver") or formal_run.get("solver"),
                "golden_model": "chiploop_python_scoreboard" if golden else "none",
            },
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
            f"- Functional bin gaps: {len(functional_gaps)}",
            f"- Code line coverage: {_status_label(summary['coverage']['code']['line_coverage_pct'], code_coverage_status)}",
            f"- Code branch coverage: {_status_label(summary['coverage']['code']['branch_coverage_pct'], code_coverage_status)}",
            f"- Code condition coverage: {_status_label(summary['coverage']['code']['condition_coverage_pct'], str(summary['coverage']['code']['condition_source']))}",
            f"- Code toggle coverage: {_status_label(summary['coverage']['code']['toggle_coverage_pct'], str(summary['coverage']['code']['toggle_source']))}",
            f"- SVA/assertion status: {assertion_status}",
            f"- SVA/assertion pass %: {_status_label(assertion_pass_pct, assertion_status)}",
            f"- Formal status: {formal_status}",
            f"- Golden model status: {golden_status}",
            f"- Simulator tool: {summary['toolchain']['simulator']}",
            f"- Code coverage tool: {summary['toolchain']['code_coverage']}",
            f"- Formal tool: {summary['toolchain']['formal']}",
            f"- Golden model tool: {summary['toolchain']['golden_model']}",
            "",
            "## Functional Coverage Not Met",
            *[
                (
                    f"- {item['coverage_point']}: bins {item['hit_bins']}/{item['total_bins']}, "
                    f"missing {', '.join(item.get('missing_bins') or ['unknown'])}, "
                    f"seen values {item.get('seen_values')}"
                )
                for item in functional_gaps[:20]
            ],
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
            "code_coverage_summary_json": code_cov_path,
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

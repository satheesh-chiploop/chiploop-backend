import json
import logging
import os
import re
import shutil
import subprocess
from datetime import datetime
from typing import Any, Dict, Optional

from utils.artifact_utils import save_text_artifact_and_record
from tooling.runner import run_command, tool_path

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


def _merge_functional_coverage_summaries(summaries: list[Dict[str, Any]]) -> Dict[str, Any]:
    if not summaries:
        return {}
    merged: Dict[str, Any] = {
        "type": "functional_coverage_summary",
        "top_module": summaries[-1].get("top_module"),
        "outputs": {},
        "inputs": {},
    }
    total_bins = 0
    bins_hit = 0
    for group_name in ("outputs", "inputs"):
        signal_names = sorted({
            name
            for summary in summaries
            for name in (summary.get(group_name) or {}).keys()
        })
        for name in signal_names:
            entries = [
                (summary.get(group_name) or {}).get(name) or {}
                for summary in summaries
                if name in (summary.get(group_name) or {})
            ]
            seen_values = sorted({
                value
                for entry in entries
                for value in (entry.get("seen_values") or [])
            })
            local_total = max((int(entry.get("total_bins") or 0) for entry in entries), default=0)
            local_hit = int(0 in seen_values) + int(any(value != 0 for value in seen_values))
            local_hit = min(local_hit, local_total)
            merged[group_name][name] = {
                "samples": sum(int(entry.get("samples") or 0) for entry in entries),
                "seen_values": seen_values,
                "hit_bins": local_hit,
                "total_bins": local_total,
            }
            total_bins += local_total
            bins_hit += local_hit
    merged["bins_hit"] = bins_hit
    merged["total_bins"] = total_bins
    merged["functional_coverage_pct"] = round(100.0 * bins_hit / max(total_bins, 1), 2)
    merged["aggregated_run_count"] = len(summaries)
    return merged


def _coverage_pct(hit: int, found: int) -> Optional[float]:
    if found <= 0:
        return None
    return round(100.0 * hit / found, 2)


def _parse_lcov_info(path: str) -> Dict[str, Any]:
    line_found = line_hit = branch_found = branch_hit = 0
    saw_da_lines = False
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for raw in f:
                line = raw.strip()
                if line.startswith("LF:"):
                    line_found += int(line.split(":", 1)[1] or 0)
                elif line.startswith("LH:"):
                    line_hit += int(line.split(":", 1)[1] or 0)
                elif line.startswith("DA:"):
                    _, payload = line.split(":", 1)
                    parts = payload.split(",")
                    if len(parts) >= 2:
                        saw_da_lines = True
                        line_found += 1
                        try:
                            if int(parts[1] or 0) > 0:
                                line_hit += 1
                        except ValueError:
                            pass
                elif line.startswith("BRF:"):
                    branch_found += int(line.split(":", 1)[1] or 0)
                elif line.startswith("BRH:"):
                    branch_hit += int(line.split(":", 1)[1] or 0)
                elif line.startswith("BRDA:"):
                    _, payload = line.split(":", 1)
                    parts = payload.split(",")
                    if len(parts) >= 4 and parts[3] not in {"", "-"}:
                        branch_found += 1
                        try:
                            if int(parts[3] or 0) > 0:
                                branch_hit += 1
                        except ValueError:
                            pass
    except Exception:
        return {}
    if saw_da_lines:
        # Verilator emits DA/BRDA records but may omit LF/LH. If BRF/BRH are also
        # present they are authoritative for branch totals, so avoid double count.
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
        if "BRF:" in text or "BRH:" in text:
            branch_found = branch_hit = 0
            for line in text.splitlines():
                if line.startswith("BRF:"):
                    branch_found += int(line.split(":", 1)[1] or 0)
                elif line.startswith("BRH:"):
                    branch_hit += int(line.split(":", 1)[1] or 0)
    branch_pct = _coverage_pct(branch_hit, branch_found)
    return {
        "line_found": line_found,
        "line_hit": line_hit,
        "line_coverage_pct": _coverage_pct(line_hit, line_found),
        "branch_found": branch_found,
        "branch_hit": branch_hit,
        "branch_coverage_pct": branch_pct,
        "condition_found": branch_found,
        "condition_hit": branch_hit,
        "condition_coverage_pct": branch_pct,
        "condition_source": "verilator_lcov_branch_proxy" if branch_found else "unavailable",
        "toggle_found": None,
        "toggle_hit": None,
        "toggle_coverage_pct": None,
        "toggle_source": "not_reported_by_verilator_lcov",
    }


def _parse_verilator_annotated_toggle_coverage(annotation_dir: str) -> Dict[str, Any]:
    toggle_found = 0
    toggle_hit = 0
    missed: list[Dict[str, Any]] = []
    point_re = re.compile(r"^[+\-]\s*(\d+)\s+point:\s+type=toggle\b(.*)$")
    for root, _, files in os.walk(annotation_dir):
        for name in files:
            path = os.path.join(root, name)
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    for raw in f:
                        match = point_re.match(raw.strip())
                        if not match:
                            continue
                        count = int(match.group(1) or 0)
                        details = match.group(2).strip()
                        toggle_found += 1
                        if count > 0:
                            toggle_hit += 1
                        elif len(missed) < 20:
                            missed.append({
                                "file": os.path.relpath(path, annotation_dir),
                                "point": details,
                            })
            except OSError:
                continue
    return {
        "toggle_found": toggle_found,
        "toggle_hit": toggle_hit,
        "toggle_coverage_pct": _coverage_pct(toggle_hit, toggle_found),
        "toggle_source": "verilator_coverage_annotate_points" if toggle_found else "not_reported_by_verilator_annotate_points",
        "missed_toggle_points": missed,
    }


def _find_verilator_coverage_data(tb_root: str) -> list[str]:
    matches: list[str] = []
    for root, _, files in os.walk(tb_root):
        for name in files:
            lower = name.lower()
            if lower in {"coverage.dat", "coverage.dat.tmp"} or lower.endswith(".dat") and "coverage" in lower:
                matches.append(os.path.join(root, name))
    return sorted(matches)


def _collect_code_coverage(
    state: Dict[str, Any],
    tb_root: str,
    reports_dir: str,
    log_path: str,
    toolchain: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    selected_tool = "verilator_coverage"
    if isinstance(toolchain, dict):
        selected_tool = str(toolchain.get("code_coverage") or selected_tool).strip().lower()
    if selected_tool in {"none", "disabled", "off"}:
        return {
            "type": "code_coverage_summary",
            "tool": selected_tool,
            "status": "disabled",
            "line_coverage_pct": None,
            "branch_coverage_pct": None,
            "condition_coverage_pct": None,
            "toggle_coverage_pct": None,
        }

    dat_files = [os.path.abspath(path) for path in _find_verilator_coverage_data(tb_root)]
    summary: Dict[str, Any] = {
        "type": "code_coverage_summary",
        "tool": selected_tool,
        "status": "missing_data",
        "coverage_dat_files": dat_files,
        "line_coverage_pct": None,
        "branch_coverage_pct": None,
        "condition_coverage_pct": None,
        "toggle_coverage_pct": None,
        "condition_source": "unavailable",
        "toggle_source": "not_reported_by_verilator_lcov",
    }
    if not dat_files:
        _log(log_path, "No Verilator coverage.dat files found", level="warning")
        return summary

    if selected_tool != "verilator_coverage":
        summary["status"] = "unsupported_tool"
        summary["supported_tools"] = ["verilator_coverage"]
        _log(log_path, f"Unsupported code coverage tool selected: {selected_tool}", level="warning")
        return summary

    verilator_coverage = tool_path("verilator_coverage", state)
    if not verilator_coverage:
        summary["status"] = "tool_unavailable"
        _log(log_path, "verilator_coverage was not found in PATH", level="warning")
        return summary

    info_path = os.path.abspath(os.path.join(reports_dir, "code_coverage.info"))
    cmd = [verilator_coverage, "--write-info", info_path] + dat_files
    try:
        proc = run_command(state, "code_coverage_lcov", cmd, cwd=tb_root, timeout_sec=120)
        summary["returncode"] = proc.returncode
        summary["stdout_tail"] = (proc.stdout or "").splitlines()[-80:]
        summary["stderr_tail"] = (proc.stderr or "").splitlines()[-80:]
        summary["tool_execution"] = proc.to_dict()
        summary["lcov_info_path"] = info_path
        if proc.returncode != 0:
            summary["status"] = "failed"
            return summary
        parsed = _parse_lcov_info(info_path)
        if not parsed:
            summary["status"] = "invalid"
            return summary
        summary.update(parsed)
        annotation_dir = os.path.abspath(os.path.join(reports_dir, "verilator_coverage_annotated"))
        shutil.rmtree(annotation_dir, ignore_errors=True)
        annotate_cmd = [
            verilator_coverage,
            "--annotate",
            annotation_dir,
            "--annotate-points",
            "--annotate-all",
            "--annotate-min",
            "1",
        ] + dat_files
        annotate_proc = run_command(state, "code_coverage_annotate", annotate_cmd, cwd=tb_root, timeout_sec=120)
        summary["annotate_returncode"] = annotate_proc.returncode
        summary["annotate_stdout_tail"] = (annotate_proc.stdout or "").splitlines()[-80:]
        summary["annotate_stderr_tail"] = (annotate_proc.stderr or "").splitlines()[-80:]
        summary["annotate_tool_execution"] = annotate_proc.to_dict()
        summary["toggle_annotation_dir"] = annotation_dir
        if annotate_proc.returncode == 0 and os.path.isdir(annotation_dir):
            summary.update(_parse_verilator_annotated_toggle_coverage(annotation_dir))
        else:
            _log(log_path, "verilator_coverage --annotate-points did not produce toggle annotation data", level="warning")
        summary["status"] = "ok"
        return summary
    except Exception as exc:
        summary["status"] = "failed"
        summary["error"] = str(exc)
        return summary


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
    agent_name = "Digital Simulation Execution Agent"

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir") or f"backend/workflows/{workflow_id}"
    tb_root = os.path.join(workflow_dir, "vv", "tb")
    reports_dir = os.path.join(tb_root, "reports")
    run_logs_dir = os.path.join(reports_dir, "run_logs")
    os.makedirs(reports_dir, exist_ok=True)
    os.makedirs(run_logs_dir, exist_ok=True)

    log_path = os.path.join("artifact", "simulation_execution_agent.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("Digital Simulation Execution Agent Log\n")

    _log(log_path, f"Starting {agent_name}...")

    try:
        manifest_path = state.get("simulation_manifest_json") or os.path.join(tb_root, "simulation_manifest.json")
        _log(log_path, f"manifest_path={manifest_path}")

        if not os.path.exists(manifest_path):
            raise FileNotFoundError("simulation_manifest.json not found")

        manifest = _safe_read_json(manifest_path)
        tests = manifest.get("default_tests", [])
        if not isinstance(tests, list):
            tests = []

        seeds = state.get("simulation_seeds")
        if not isinstance(seeds, list) or not seeds:
            seed_count = state.get("seed_count")
            try:
                seed_count = max(int(seed_count), 1)
            except (TypeError, ValueError):
                seed_count = 1
            seeds = list(range(1, seed_count + 1))

        _log_kv(log_path, "tests", tests)
        _log_kv(log_path, "seeds", seeds)

        coverage_json_path = manifest.get("functional_coverage_summary_json") or os.path.join(
            reports_dir, "functional_coverage_summary.json"
        )
        coverage_md_path = manifest.get("functional_coverage_md") or os.path.join(
            reports_dir, "COVERAGE.md"
        )
        results = []
        runtime_coverage_summaries: list[Dict[str, Any]] = []

        for t in tests:
            for s in seeds:
                _log(log_path, f"Running testcase={t} seed={s}")

                env = os.environ.copy()
                env["RANDOM_SEED"] = str(s)

                try:
                    p = run_command(
                        state,
                        "digital_simulation",
                        ["make", f"TESTCASE={t}"],
                        cwd=tb_root,
                        env=env,
                    )

                    stdout_tail = (p.stdout or "").splitlines()[-120:]
                    stderr_tail = (p.stderr or "").splitlines()[-120:]

                    _write_file(
                        os.path.join(run_logs_dir, f"{t}__seed_{s}.stdout.log"),
                        "\n".join(stdout_tail) + "\n",
                    )
                    _write_file(
                        os.path.join(run_logs_dir, f"{t}__seed_{s}.stderr.log"),
                        "\n".join(stderr_tail) + "\n",
                    )

                    results.append({
                        "testcase": t,
                        "seed": s,
                        "pass": p.returncode == 0,
                        "rc": p.returncode,
                        "stdout_tail": stdout_tail,
                        "stderr_tail": stderr_tail,
                        "tool_execution": p.to_dict(),
                    })
                    run_coverage = _safe_read_json(coverage_json_path)
                    if run_coverage:
                        runtime_coverage_summaries.append(run_coverage)

                except Exception as e:
                    _log(log_path, f"Execution failed testcase={t} seed={s}: {e}", level="error")
                    results.append({
                        "testcase": t,
                        "seed": s,
                        "pass": False,
                        "error": str(e),
                    })

        aggregated_coverage = _merge_functional_coverage_summaries(runtime_coverage_summaries)
        if aggregated_coverage:
            _write_file(coverage_json_path, json.dumps(aggregated_coverage, indent=2))

        coverage_json_present = os.path.exists(coverage_json_path)
        coverage_md_present = os.path.exists(coverage_md_path)

        artifacts = {}

        if coverage_json_present:
            artifacts["functional_coverage_summary"] = _record_text(
                workflow_id, agent_name, "vv/tb/reports",
                "functional_coverage_summary.json",
                open(coverage_json_path).read()
            )

        if coverage_md_present:
            artifacts["functional_coverage_md"] = _record_text(
                workflow_id, agent_name, "vv/tb/reports",
                "COVERAGE.md",
                open(coverage_md_path).read()
            )

        if not coverage_json_present:
            _log(log_path, f"Missing runtime coverage JSON: {coverage_json_path}", level="warning")
        if not coverage_md_present:
            _log(log_path, f"Missing runtime coverage markdown: {coverage_md_path}", level="warning")

        toolchain = state.get("toolchain") if isinstance(state.get("toolchain"), dict) else {}
        code_coverage = _collect_code_coverage(state, tb_root, reports_dir, log_path, toolchain)
        code_coverage_path = os.path.join(reports_dir, "code_coverage_summary.json")
        code_coverage_txt = json.dumps(code_coverage, indent=2)
        _write_file(code_coverage_path, code_coverage_txt)
        artifacts["code_coverage_summary"] = _record_text(
            workflow_id,
            agent_name,
            "vv/tb/reports",
            "code_coverage_summary.json",
            code_coverage_txt,
        )
        info_path = code_coverage.get("lcov_info_path")
        if isinstance(info_path, str) and os.path.exists(info_path):
            artifacts["code_coverage_info"] = _record_text(
                workflow_id,
                agent_name,
                "vv/tb/reports",
                "code_coverage.info",
                open(info_path, "r", encoding="utf-8", errors="ignore").read(),
            )

        summary = {
            "type": "simulation_execution_summary",
            "total": len(results),
            "pass": sum(1 for r in results if r.get("pass")),
            "fail": sum(1 for r in results if not r.get("pass")),
            "coverage_json_present": coverage_json_present,
            "coverage_md_present": coverage_md_present,
            "coverage_json_path": coverage_json_path,
            "coverage_md_path": coverage_md_path,
            "code_coverage_json_present": os.path.exists(code_coverage_path),
            "code_coverage_json_path": code_coverage_path,
            "code_coverage": code_coverage,
            "toolchain": {
                "simulator": toolchain.get("simulator") or state.get("simulator_type") or "verilator",
                "code_coverage": toolchain.get("code_coverage") or "verilator_coverage",
            },
            "results": results,
        }

        
        summary_txt = json.dumps(summary, indent=2)

        md_lines = [
            "# Simulation Execution Summary",
            "",
            f"- Total runs: {summary['total']}",
            f"- Pass count: {summary['pass']}",
            f"- Fail count: {summary['fail']}",
            "",
            "## Results",
        ]
        for r in results:
            md_lines.append(
                f"- {r.get('testcase')} / seed {r.get('seed')}: "
                f"{'PASS' if r.get('pass') else 'FAIL'} (rc={r.get('rc')})"
            )
        md_txt = "\n".join(md_lines) + "\n"

        summary_path = os.path.join(reports_dir, "simulation_execution_summary.json")
        md_path = os.path.join(reports_dir, "SIM_EXECUTION.md")
        _write_file(summary_path, summary_txt)
        _write_file(md_path, md_txt)

        artifacts["simulation_execution_summary"] = _record_text(
            workflow_id, agent_name, "vv/tb/reports", "simulation_execution_summary.json", summary_txt
        )
        artifacts["sim_execution_md"] = _record_text(
            workflow_id, agent_name, "vv/tb/reports", "SIM_EXECUTION.md", md_txt
        )


        report = {
            "type": "simulation_execution_report",
            "manifest_path": manifest_path,
            "tests": tests,
            "seeds": seeds,
            "pass": summary["pass"],
            "fail": summary["fail"],
            "coverage_json_present": summary["coverage_json_present"],
            "coverage_md_present": summary["coverage_md_present"],
            "code_coverage_json_present": summary["code_coverage_json_present"],
            "code_coverage": code_coverage,
            "toolchain": summary["toolchain"],
            "artifacts": artifacts,
        }
        rep_txt = json.dumps(report, indent=2)
        report_path = os.path.join(reports_dir, "simulation_execution_report.json")
        _write_file(report_path, rep_txt)

        artifacts["report"] = _record_text(
            workflow_id, agent_name, "vv/tb/reports", "simulation_execution_report.json", rep_txt
        )

        try:
            with open(log_path, "r", encoding="utf-8") as f:
                log_text = f.read()
        except Exception:
            log_text = ""
        artifacts["log"] = _record_text(
            workflow_id, agent_name, "vv", "simulation_execution_agent.log", log_text
        )

        _log(log_path, f"Execution summary written: {summary_path}")

        state["simulation_execution_summary_json"] = summary_path
        state["simulation_execution_report_json"] = report_path
        state["code_coverage_summary_json"] = code_coverage_path
        state.setdefault("vv", {})
        state["vv"]["simulation_execution"] = report

        _log(log_path, f"{agent_name} completed successfully.")
        return state

    except Exception as e:
        _log(log_path, f"{agent_name} failed: {e}", level="error")
        raise

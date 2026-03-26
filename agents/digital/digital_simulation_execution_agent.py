import json
import logging
import os
import subprocess
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

        seeds = state.get("simulation_seeds", [1])
        if not isinstance(seeds, list):
            seeds = [1]

        _log_kv(log_path, "tests", tests)
        _log_kv(log_path, "seeds", seeds)

        results = []

        for t in tests:
            for s in seeds:
                _log(log_path, f"Running testcase={t} seed={s}")

                env = os.environ.copy()
                env["RANDOM_SEED"] = str(s)

                try:
                    p = subprocess.run(
                        ["make", f"TESTCASE={t}"],
                        cwd=tb_root,
                        capture_output=True,
                        text=True,
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
                    })

                except Exception as e:
                    _log(log_path, f"Execution failed testcase={t} seed={s}: {e}", level="error")
                    results.append({
                        "testcase": t,
                        "seed": s,
                        "pass": False,
                        "error": str(e),
                    })


        coverage_json_path = manifest.get("functional_coverage_summary_json") or os.path.join(
            reports_dir, "functional_coverage_summary.json"
        )
        coverage_md_path = manifest.get("functional_coverage_md") or os.path.join(
            reports_dir, "COVERAGE.md"
        )

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

        summary = {
            "type": "simulation_execution_summary",
            "total": len(results),
            "pass": sum(1 for r in results if r.get("pass")),
            "fail": sum(1 for r in results if not r.get("pass")),
            "coverage_json_present": coverage_json_present,
            "coverage_md_present": coverage_md_present,
            "coverage_json_path": coverage_json_path,
            "coverage_md_path": coverage_md_path,
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
        state.setdefault("vv", {})
        state["vv"]["simulation_execution"] = report

        _log(log_path, f"{agent_name} completed successfully.")
        return state

    except Exception as e:
        _log(log_path, f"{agent_name} failed: {e}", level="error")
        raise
import os
import re
import json
import glob
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from utils.artifact_utils import save_text_artifact_and_record


def _now() -> str:
    return datetime.now().isoformat()


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _record(workflow_id: str, agent_name: str, subdir: str, filename: str, content: str) -> Optional[str]:
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


def _safe_read_json(path: str) -> Dict[str, Any]:
    try:
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                obj = json.load(f)
                return obj if isinstance(obj, dict) else {}
    except Exception:
        pass
    return {}


def _find_first(paths: List[str]) -> Optional[str]:
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None


def _extract_pct_from_text(text: str) -> Optional[float]:
    if not text:
        return None

    patterns = [
        r"([0-9]+(?:\.[0-9]+)?)\s*%",
        r"coverage[^0-9]*([0-9]+(?:\.[0-9]+)?)",
        r"covered[^0-9]*([0-9]+(?:\.[0-9]+)?)",
    ]
    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            try:
                v = float(m.group(1))
                if 0.0 <= v <= 100.0:
                    return v
            except Exception:
                pass
    return None


def _extract_hits_total_from_text(text: str) -> Tuple[Optional[int], Optional[int]]:
    if not text:
        return None, None

    patterns = [
        r"bins[_ ]?hit[^0-9]*([0-9]+)[^0-9]+total[_ ]?bins[^0-9]*([0-9]+)",
        r"covered[^0-9]*([0-9]+)[^0-9]+of[^0-9]+([0-9]+)",
        r"hits[^0-9]*([0-9]+)[^0-9]+total[^0-9]*([0-9]+)",
    ]
    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE | re.DOTALL)
        if m:
            try:
                return int(m.group(1)), int(m.group(2))
            except Exception:
                pass
    return None, None


def _find_candidate_files(workflow_dir: str) -> Dict[str, List[str]]:
    pats = {
        "functional_json": [
            "**/*functional*coverage*.json",
            "**/*coverage*summary*.json",
            "**/*coverage*.json",
        ],
        "functional_md": [
            "**/COVERAGE.md",
            "**/*coverage*.md",
        ],
        "code_logs": [
            "**/*.log",
            "**/*.txt",
        ],
    }

    out: Dict[str, List[str]] = {}
    for k, globs_ in pats.items():
        acc: List[str] = []
        for pat in globs_:
            acc.extend(glob.glob(os.path.join(workflow_dir, pat), recursive=True))
        out[k] = sorted(list(dict.fromkeys([p for p in acc if os.path.isfile(p)])))
    return out


def _functional_coverage(workflow_dir: str) -> Dict[str, Any]:
    cands = _find_candidate_files(workflow_dir)
    source = None

    # Prefer JSON if present
    for p in cands["functional_json"]:
        obj = _safe_read_json(p)
        if not obj:
            continue

        # common shapes
        if "functional_coverage_pct" in obj:
            return {
                "functional_coverage_pct": obj.get("functional_coverage_pct"),
                "bins_hit": obj.get("bins_hit"),
                "total_bins": obj.get("total_bins"),
                "source": p,
            }

        for key in ["coverage_pct", "coverage", "percent"]:
            if key in obj:
                try:
                    v = float(obj[key])
                    if 0.0 <= v <= 100.0:
                        return {
                            "functional_coverage_pct": v,
                            "bins_hit": obj.get("bins_hit"),
                            "total_bins": obj.get("total_bins"),
                            "source": p,
                        }
                except Exception:
                    pass

        # nested metrics
        cov = obj.get("coverage") or obj.get("functional_coverage")
        if isinstance(cov, dict):
            pct = cov.get("pct") or cov.get("coverage_pct") or cov.get("percent")
            if pct is not None:
                try:
                    return {
                        "functional_coverage_pct": float(pct),
                        "bins_hit": cov.get("bins_hit") or cov.get("hit"),
                        "total_bins": cov.get("total_bins") or cov.get("total"),
                        "source": p,
                    }
                except Exception:
                    pass

    # Fallback to markdown/text parsing
    for p in cands["functional_md"]:
        try:
            txt = open(p, "r", encoding="utf-8", errors="ignore").read()
            pct = _extract_pct_from_text(txt)
            hit, total = _extract_hits_total_from_text(txt)
            if pct is not None or hit is not None or total is not None:
                if pct is None and hit is not None and total:
                    pct = round(100.0 * hit / total, 2)
                return {
                    "functional_coverage_pct": pct,
                    "bins_hit": hit,
                    "total_bins": total,
                    "source": p,
                }
        except Exception:
            pass

    return {
        "functional_coverage_pct": None,
        "bins_hit": None,
        "total_bins": None,
        "source": None,
    }


def _code_coverage(workflow_dir: str) -> Dict[str, Any]:
    cands = _find_candidate_files(workflow_dir)

    # Best-effort from logs / coverage files
    for p in cands["code_logs"]:
        try:
            txt = open(p, "r", encoding="utf-8", errors="ignore").read()
        except Exception:
            continue

        if "coverage" not in txt.lower():
            continue

        pct = _extract_pct_from_text(txt)
        if pct is not None:
            return {
                "code_coverage_pct": pct,
                "source": p,
            }

    # If no explicit percent found but coverage artifacts exist, mark unavailable rather than invent a number
    dats = glob.glob(os.path.join(workflow_dir, "**", "*.dat"), recursive=True)
    if dats:
        return {
            "code_coverage_pct": None,
            "source": dats[0],
        }

    return {
        "code_coverage_pct": None,
        "source": None,
    }


def _assertion_coverage(workflow_dir: str, execution: Dict[str, Any]) -> Dict[str, Any]:
    total = execution.get("assertions_total")
    failures = execution.get("assertion_failures_total")

    try:
        total_i = int(total) if total is not None else 0
    except Exception:
        total_i = 0

    try:
        fail_i = int(failures) if failures is not None else 0
    except Exception:
        fail_i = 0

    # Demo-friendly best-effort metric:
    # if assertions were compiled and no failures occurred across runs, mark exercised pass-rate as 100.
    # If failures occurred, decrement proportionally. This is not full UCIS assertion coverage.
    if total_i > 0:
        exercised = max(total_i - min(fail_i, total_i), 0)
        pct = round(100.0 * exercised / total_i, 2)
        return {
            "assertion_coverage_pct": pct,
            "assertions_total": total_i,
            "assertion_failures": fail_i,
            "source": "execution_logs_best_effort",
            "note": "Demo best-effort metric from assertion compile presence + runtime failures; not full UCIS assertion coverage.",
        }

    return {
        "assertion_coverage_pct": None,
        "assertions_total": 0,
        "assertion_failures": fail_i,
        "source": None,
        "note": "No assertion collateral detected.",
    }


def run_agent(state: dict) -> dict:
    agent_name = "System Simulation Coverage Summary Agent"
    print("\n📊 Running System Simulation Coverage Summary Agent...")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    out_root = os.path.join(workflow_dir, "system", "sim")
    os.makedirs(out_root, exist_ok=True)

    exec_json_path = _find_first([
        os.path.join(out_root, "system_sim_execution.json"),
    ])
    execution = _safe_read_json(exec_json_path) if exec_json_path else (state.get("system_sim_execution") or {})

    if not execution:
        state["status"] = "❌ Missing system_sim_execution.json. Run System Simulation Execution Agent first."
        return state

    func_cov = _functional_coverage(workflow_dir)
    code_cov = _code_coverage(workflow_dir)
    asrt_cov = _assertion_coverage(workflow_dir, execution)

    dashboard = {
        "type": "system_sim_dashboard",
        "version": "1.0",
        "generated_at": _now(),
        "top_module": execution.get("top_module"),
        "tests_run": execution.get("tests_run"),
        "tests_passed": execution.get("tests_passed"),
        "tests_failed": execution.get("tests_failed"),
        "total_runtime_sec": execution.get("total_runtime_sec"),
        "functional_coverage_pct": func_cov.get("functional_coverage_pct"),
        "functional_bins_hit": func_cov.get("bins_hit"),
        "functional_total_bins": func_cov.get("total_bins"),
        "functional_coverage_source": func_cov.get("source"),
        "code_coverage_pct": code_cov.get("code_coverage_pct"),
        "code_coverage_source": code_cov.get("source"),
        "assertion_coverage_pct": asrt_cov.get("assertion_coverage_pct"),
        "assertions_total": asrt_cov.get("assertions_total"),
        "assertion_failures": asrt_cov.get("assertion_failures"),
        "assertion_coverage_source": asrt_cov.get("source"),
        "assertion_coverage_note": asrt_cov.get("note"),
        "waveforms": execution.get("waveforms") or [],
        "run_matrix": execution.get("matrix") or {},
        "runs": execution.get("runs") or [],
    }

    md = [
        "# System Simulation Coverage Summary",
        "",
        f"- Top: `{dashboard.get('top_module')}`",
        f"- Tests run: {dashboard.get('tests_run')}",
        f"- Passed: {dashboard.get('tests_passed')}",
        f"- Failed: {dashboard.get('tests_failed')}",
        f"- Functional coverage: {dashboard.get('functional_coverage_pct')}",
        f"- Code coverage: {dashboard.get('code_coverage_pct')}",
        f"- Assertion coverage: {dashboard.get('assertion_coverage_pct')}",
        "",
        "## Coverage Detail",
        f"- Functional bins hit: {dashboard.get('functional_bins_hit')}",
        f"- Functional total bins: {dashboard.get('functional_total_bins')}",
        f"- Assertions total: {dashboard.get('assertions_total')}",
        f"- Assertion failures: {dashboard.get('assertion_failures')}",
        "",
        "## Notes",
        f"- Functional source: `{dashboard.get('functional_coverage_source')}`",
        f"- Code source: `{dashboard.get('code_coverage_source')}`",
        f"- Assertion source: `{dashboard.get('assertion_coverage_source')}`",
        f"- Assertion note: {dashboard.get('assertion_coverage_note')}",
    ]
    md_txt = "\n".join(md) + "\n"
    js_txt = json.dumps(dashboard, indent=2)

    _write(os.path.join(out_root, "system_sim_dashboard.json"), js_txt)
    _write(os.path.join(out_root, "system_sim_dashboard.md"), md_txt)

    _record(workflow_id, agent_name, "system/sim", "system_sim_dashboard.json", js_txt)
    _record(workflow_id, agent_name, "system/sim", "system_sim_dashboard.md", md_txt)

    state.setdefault("system_sim", {})
    state["system_sim"]["dashboard"] = dashboard
    state["system_sim_dashboard"] = dashboard
    state["status"] = (
        f"✅ System simulation summary ready: "
        f"func={dashboard.get('functional_coverage_pct')}, "
        f"code={dashboard.get('code_coverage_pct')}, "
        f"assert={dashboard.get('assertion_coverage_pct')}"
    )
    return state
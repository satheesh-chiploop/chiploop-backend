import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "Digital Coverage Gap Analysis Agent"


def _num(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) else None


def _target(text: Any, kind: str, default: float) -> float:
    raw = str(text or "")
    patterns = {
        "functional": r"(\d+(?:\.\d+)?)\s*%?\s*(?:functional|func)",
        "line": r"(\d+(?:\.\d+)?)\s*%?\s*(?:line|code)",
        "branch": r"(\d+(?:\.\d+)?)\s*%?\s*branch",
        "condition": r"(\d+(?:\.\d+)?)\s*%?\s*condition",
        "toggle": r"(\d+(?:\.\d+)?)\s*%?\s*toggle",
    }
    m = re.search(patterns[kind], raw, re.IGNORECASE)
    return float(m.group(1)) if m else default


def _functional_gaps(cov: Dict[str, Any]) -> List[Dict[str, Any]]:
    gaps: List[Dict[str, Any]] = []
    for group in ("outputs", "inputs"):
        entries = cov.get(group) if isinstance(cov.get(group), dict) else {}
        for name, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            hit = int(entry.get("hit_bins") or 0)
            total = int(entry.get("total_bins") or 0)
            if total > 0 and hit < total:
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
                    "recommendation": (
                        f"Add directed stimulus and checker sampling for {group}.{name}; "
                        f"target missing bins: {', '.join(missing_bins) if missing_bins else 'unknown'}."
                    ),
                })
    return gaps


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    workflow_id = str(state.get("workflow_id") or "default")
    workflow_dir = Path(str(state.get("workflow_dir") or f"backend/workflows/{workflow_id}"))
    out_dir = workflow_dir / "verify_closure"
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = state.get("source_simulation_summary_coverage") if isinstance(state.get("source_simulation_summary_coverage"), dict) else {}
    coverage = summary.get("coverage") if isinstance(summary.get("coverage"), dict) else {}
    functional = coverage.get("functional") if isinstance(coverage.get("functional"), dict) else {}
    code = coverage.get("code") if isinstance(coverage.get("code"), dict) else {}
    functional_raw = state.get("source_functional_coverage_summary") if isinstance(state.get("source_functional_coverage_summary"), dict) else {}

    functional_pct = _num(coverage.get("functional_coverage_pct")) or _num(functional.get("coverage_pct"))
    line_pct = _num(code.get("line_coverage_pct"))
    branch_pct = _num(code.get("branch_coverage_pct"))
    condition_pct = _num(code.get("condition_coverage_pct"))
    toggle_pct = _num(code.get("toggle_coverage_pct"))
    targets = {
        "functional": _target(state.get("coverage_targets"), "functional", 100.0),
        "line": _target(state.get("coverage_targets"), "line", 90.0),
        "branch": _target(state.get("coverage_targets"), "branch", 80.0),
        "condition": _target(state.get("coverage_targets"), "condition", 80.0),
        "toggle": _target(state.get("coverage_targets"), "toggle", 80.0),
    }

    gaps: List[Dict[str, Any]] = []
    if functional_pct is None:
        gaps.append({"type": "functional_coverage_missing", "recommendation": "Confirm the coverage model was imported and sampled during simulation."})
    elif functional_pct < targets["functional"]:
        gaps.append({
            "type": "functional_coverage_below_target",
            "actual": functional_pct,
            "target": targets["functional"],
            "recommendation": "Generate directed tests for unhit functional bins before only increasing random seeds.",
        })
    if line_pct is None:
        gaps.append({"type": "code_line_coverage_unavailable", "recommendation": "Enable Verilator code coverage and confirm verilator_coverage is in PATH."})
    elif line_pct < targets["line"]:
        gaps.append({
            "type": "code_line_coverage_below_target",
            "actual": line_pct,
            "target": targets["line"],
            "recommendation": "Inspect uncovered lines; classify as missing stimulus, unreachable code, or RTL cleanup candidate.",
        })
    if branch_pct is not None and branch_pct < targets["branch"]:
        gaps.append({
            "type": "code_branch_coverage_below_target",
            "actual": branch_pct,
            "target": targets["branch"],
            "recommendation": "Add branch-directed tests around control conditions and error/edge paths.",
        })
    if condition_pct is None:
        gaps.append({
            "type": "code_condition_coverage_unavailable",
            "recommendation": "Use a coverage backend that exports condition coverage, or review Verilator branch coverage as a condition proxy.",
        })
    elif condition_pct < targets["condition"]:
        gaps.append({
            "type": "code_condition_coverage_below_target",
            "actual": condition_pct,
            "target": targets["condition"],
            "recommendation": "Add tests that exercise each boolean condition outcome in control logic.",
        })
    if toggle_pct is None:
        gaps.append({
            "type": "code_toggle_coverage_unavailable",
            "recommendation": "Enable a simulator coverage flow that exports toggle coverage, or add toggle extraction from the native coverage database.",
        })
    elif toggle_pct < targets["toggle"]:
        gaps.append({
            "type": "code_toggle_coverage_below_target",
            "actual": toggle_pct,
            "target": targets["toggle"],
            "recommendation": "Add tests that toggle idle outputs, status bits, interrupts, and register-controlled datapath signals.",
        })
    functional_bin_gaps = _functional_gaps(functional_raw)
    gaps.extend(functional_bin_gaps[:20])

    seed_count = int(state.get("seed_count") or 0)
    actions = []
    if gaps:
        actions.append({
            "action": "add_directed_tests",
            "priority": "high",
            "why": "Coverage holes should first be mapped to explicit scenarios so closure is explainable.",
        })
        if seed_count and seed_count < 25:
            actions.append({
                "action": "increase_random_seeds",
                "priority": "medium",
                "suggested_seed_count": max(25, seed_count * 3),
                "why": "More seeds may close random-observable bins after directed gaps are handled.",
            })
        actions.append({
            "action": "review_coverage_model",
            "priority": "medium",
            "why": "Unreachable or incorrectly specified bins should be waived or corrected, not chased indefinitely.",
        })

    report = {
        "type": "coverage_gap_analysis",
        "targets": targets,
        "actual": {
            "functional_coverage_pct": functional_pct,
            "code_line_coverage_pct": line_pct,
            "code_branch_coverage_pct": branch_pct,
            "code_condition_coverage_pct": condition_pct,
            "code_toggle_coverage_pct": toggle_pct,
            "code_condition_source": code.get("condition_source"),
            "code_toggle_source": code.get("toggle_source"),
        },
        "gap_count": len(gaps),
        "functional_gap_count": len(functional_bin_gaps),
        "functional_gaps": functional_bin_gaps,
        "gaps": gaps,
        "recommended_actions": actions,
    }
    txt = json.dumps(report, indent=2)
    md = "\n".join([
        "# Coverage Gap Analysis",
        "",
        f"- Functional coverage: {functional_pct if functional_pct is not None else 'unavailable'} / target {targets['functional']}%",
        f"- Code line coverage: {line_pct if line_pct is not None else 'unavailable'} / target {targets['line']}%",
        f"- Code branch coverage: {branch_pct if branch_pct is not None else 'unavailable'} / target {targets['branch']}%",
        f"- Code condition coverage: {condition_pct if condition_pct is not None else 'unavailable'} / target {targets['condition']}%",
        f"- Code toggle coverage: {toggle_pct if toggle_pct is not None else 'unavailable'} / target {targets['toggle']}%",
        f"- Gap count: {len(gaps)}",
        f"- Functional bin gaps: {len(functional_bin_gaps)}",
        "",
        "## Functional Coverage Gaps",
        *[
            (
                f"- {item['coverage_point']}: bins {item['hit_bins']}/{item['total_bins']}, "
                f"missing {', '.join(item.get('missing_bins') or ['unknown'])}, "
                f"seen values {item.get('seen_values')}"
            )
            for item in functional_bin_gaps[:20]
        ],
        "",
        "## Recommended Actions",
        *[f"- {item['priority']}: {item['action']} - {item['why']}" for item in actions],
    ]) + "\n"
    (out_dir / "coverage_gap_analysis.json").write_text(txt, encoding="utf-8")
    (out_dir / "coverage_gap_analysis.md").write_text(md, encoding="utf-8")
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "verify_closure", "coverage_gap_analysis.json", txt)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "verify_closure", "coverage_gap_analysis.md", md)
    state["coverage_gap_analysis"] = report
    return state

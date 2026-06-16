import json
import os
import re
from typing import Any
from xml.etree import ElementTree

from utils.artifact_utils import save_text_artifact_and_record


AGENT_NAME = "System PD Signoff Closure Agent"
OUT_DIR = os.path.join("digital", "signoff_closure")

STAGE_RANK = {
    "System Top Assembly Agent": 10,
    "Analog Physical Collateral Package Agent": 20,
    "Digital Synthesis Agent": 30,
    "Digital Logic Equivalence Check Agent": 35,
    "Digital DFT Scan Stitching Agent": 40,
    "Digital STA PrePlace Agent": 50,
    "Digital Floorplan Agent": 60,
    "Digital Placement Agent": 70,
    "Digital CTS Agent": 80,
    "Digital Route Agent": 90,
    "Digital Fill Agent": 100,
    "Digital STA PostFill Agent": 110,
    "Digital DRC Agent": 120,
    "Digital LVS Agent": 130,
    "Digital Tapeout Agent": 140,
    "Digital Tapeout Logic Equivalence Check Agent": 150,
}
PD_RESTART_MIN_RANK = STAGE_RANK["Digital STA PrePlace Agent"]


def _read_json(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception:
        return ""


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _first_number(*values: Any) -> float | int | None:
    for value in values:
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return value
        try:
            return float(str(value).strip())
        except Exception:
            continue
    return None


def _status_clean(status: Any, clean_values: set[str]) -> bool:
    return str(status or "").strip().lower() in clean_values


def _known_status(*values: Any) -> str:
    raw = _first_present(*values)
    status = str(raw or "").strip().lower()
    return "" if status in {"", "none", "null", "not_produced", "not produced"} else status


def _enabled(state: dict[str, Any]) -> bool:
    toggles = state.get("toggles") if isinstance(state.get("toggles"), dict) else {}
    closure = state.get("signoff_closure") if isinstance(state.get("signoff_closure"), dict) else {}
    return bool(
        _first_present(
            closure.get("enabled"),
            state.get("run_signoff_closure_loop"),
            toggles.get("run_signoff_closure_loop"),
            False,
        )
    )


def _max_iterations(state: dict[str, Any]) -> int:
    closure = state.get("signoff_closure") if isinstance(state.get("signoff_closure"), dict) else {}
    raw = _first_present(
        closure.get("max_iterations"),
        state.get("max_signoff_closure_iterations"),
        state.get("max_signoff_iterations"),
        1,
    )
    try:
        value = int(raw)
    except Exception:
        value = 1
    return max(1, min(2, value))


def _repair_enabled(state: dict[str, Any], key: str) -> bool:
    toggles = state.get("toggles") if isinstance(state.get("toggles"), dict) else {}
    closure = state.get("signoff_closure") if isinstance(state.get("signoff_closure"), dict) else {}
    return bool(_first_present(closure.get(key), state.get(key), toggles.get(key), True))


def _load_artifacts(workflow_dir: str) -> dict[str, dict[str, Any]]:
    paths = {
        "executive": os.path.join(workflow_dir, "digital", "executive_summary.json"),
        "synth": os.path.join(workflow_dir, "digital", "synth", "metrics.json"),
        "lec": os.path.join(workflow_dir, "digital", "lec", "lec_summary.json"),
        "tapeout_lec": os.path.join(workflow_dir, "digital", "tapeout_lec", "tapeout_lec_summary.json"),
        "drc": os.path.join(workflow_dir, "digital", "drc", "drc_summary.json"),
        "lvs": os.path.join(workflow_dir, "digital", "lvs", "lvs_summary.json"),
        "tapeout": os.path.join(workflow_dir, "digital", "tapeout", "tapeout_summary.json"),
        "sta_postfill": os.path.join(workflow_dir, "digital", "sta_postfill", "metrics.json"),
        "sta_postfill_summary": os.path.join(workflow_dir, "digital", "sta_postfill", "sta_postfill_summary.json"),
        "sta_postroute": os.path.join(workflow_dir, "digital", "sta_postroute", "metrics.json"),
        "analog_physical_package": os.path.join(workflow_dir, "analog", "physical_package", "analog_physical_collateral_package.json"),
        "analog_signoff": os.path.join(workflow_dir, "analog", "signoff", "analog_signoff_summary.json"),
    }
    return {key: _read_json(path) for key, path in paths.items()}


def _drc_category_counts(workflow_dir: str) -> dict[str, int]:
    report_dir = os.path.join(workflow_dir, "digital", "drc", "reports")
    if not os.path.isdir(report_dir):
        return {}
    reports = [
        os.path.join(report_dir, name)
        for name in os.listdir(report_dir)
        if name.lower().endswith(".xml")
    ]
    if not reports:
        return {}
    reports.sort(key=lambda path: os.path.getmtime(path))
    try:
        root = ElementTree.parse(reports[-1]).getroot()
    except Exception:
        return {}
    counts: dict[str, int] = {}
    for item in root.findall(".//item"):
        category = None
        for tag in ("category", "cell", "source"):
            elem = item.find(tag)
            if elem is not None and elem.text:
                category = elem.text.strip()
                break
        if not category:
            values = [v.text.strip() for v in item.findall("value") if v.text]
            category = values[0] if values else "unknown"
        category = category.split()[0].strip() or "unknown"
        counts[category] = counts.get(category, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))


def _drc_restart_stage(category_counts: dict[str, int]) -> tuple[str, str]:
    names = " ".join(category_counts.keys()).lower()
    if re.search(r"\b(nwell|pwell|tap|difftap|metaltap|macro|boundary|obs|obstruction|pdn|power)\b", names):
        return "Digital Floorplan Agent", "DRC categories point at wells/taps/macros/PDN/floorplan geometry."
    if re.search(r"\b(poly|licon|mcon|contact|li|via|met[1-5]|m[1-5])\b", names):
        return "Digital Route Agent", "DRC categories are local routing/contact/metal classes."
    return "Digital DRC Agent", "DRC violations need report-specific triage before choosing an earlier physical stage."


def _sta_metrics(artifacts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    postfill = artifacts.get("sta_postfill") or {}
    postfill_summary = artifacts.get("sta_postfill_summary") or {}
    executive = artifacts.get("executive") or {}
    stages = executive.get("sta_stages") if isinstance(executive.get("sta_stages"), dict) else {}
    exec_postfill = stages.get("sta_postfill") if isinstance(stages.get("sta_postfill"), dict) else {}
    wns = _first_number(
        postfill.get("worst_slack"),
        postfill.get("timing__setup__ws"),
        postfill_summary.get("worst_slack"),
        exec_postfill.get("worst_slack"),
    )
    tns = _first_number(
        postfill.get("tns"),
        postfill.get("timing__setup__tns"),
        postfill_summary.get("tns"),
        exec_postfill.get("tns"),
    )
    setup_wns = _first_number(
        postfill.get("setup_wns"),
        postfill.get("timing__setup__wns"),
        postfill.get("timing__setup__ws"),
        postfill.get("worst_slack"),
        postfill_summary.get("setup_wns"),
        postfill_summary.get("worst_slack"),
        exec_postfill.get("setup_wns"),
        exec_postfill.get("worst_slack"),
    )
    setup_tns = _first_number(
        postfill.get("setup_tns"),
        postfill.get("timing__setup__tns"),
        postfill.get("tns"),
        postfill_summary.get("setup_tns"),
        postfill_summary.get("tns"),
        exec_postfill.get("setup_tns"),
        exec_postfill.get("tns"),
    )
    hold_wns = _first_number(
        postfill.get("hold_wns"),
        postfill.get("timing__hold__wns"),
        postfill.get("timing__hold__ws"),
        postfill_summary.get("hold_wns"),
        exec_postfill.get("hold_wns"),
    )
    hold_tns = _first_number(
        postfill.get("hold_tns"),
        postfill.get("timing__hold__tns"),
        postfill_summary.get("hold_tns"),
        exec_postfill.get("hold_tns"),
    )
    setup_violations = _first_number(
        postfill.get("setup_violations"),
        postfill.get("timing__setup__violation_count"),
        postfill.get("timing__setup__vio__count"),
        postfill.get("timing__setup_vio__count"),
        postfill.get("timing__setup_r2r_vio__count"),
        postfill.get("timing__setup__violating_paths"),
        postfill.get("sta__setup__violation_count"),
        postfill_summary.get("setup_violations"),
    )
    hold_violations = _first_number(
        postfill.get("hold_violations"),
        postfill.get("timing__hold__violation_count"),
        postfill.get("timing__hold__vio__count"),
        postfill.get("timing__hold_vio__count"),
        postfill.get("timing__hold_r2r_vio__count"),
        postfill.get("timing__hold__violating_paths"),
        postfill.get("sta__hold__violation_count"),
        postfill_summary.get("hold_violations"),
    )
    status = _first_present(postfill_summary.get("status"), postfill.get("status"))
    if setup_violations is None and _status_clean(status, {"ok", "clean", "pass", "completed", "generated"}):
        if setup_tns == 0 or (setup_wns is not None and setup_wns >= 0):
            setup_violations = 0
    if hold_violations is None and _status_clean(status, {"ok", "clean", "pass", "completed", "generated"}):
        if hold_tns == 0 or (hold_wns is not None and hold_wns >= 0):
            hold_violations = 0
    return {
        "stage": "sta_postfill" if postfill or postfill_summary or exec_postfill else None,
        "wns": wns,
        "tns": tns,
        "setup_wns": setup_wns,
        "setup_tns": setup_tns,
        "hold_wns": hold_wns,
        "hold_tns": hold_tns,
        "setup_violations": setup_violations,
        "hold_violations": hold_violations,
        "status": status,
    }


def _drc_metrics(artifacts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    drc = artifacts.get("drc") or {}
    status = _first_present(drc.get("drc_status"), drc.get("status"))
    violations = _first_number(drc.get("drc_violations"), drc.get("violations"))
    if violations is None and _status_clean(status, {"ok", "clean", "pass", "completed"}):
        violations = 0
    return {
        "status": status,
        "violations": violations,
    }


def _lvs_metrics(artifacts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    lvs = artifacts.get("lvs") or {}
    return {
        "status": _first_present(lvs.get("lvs_status"), lvs.get("status")),
        "reason": _first_present(lvs.get("failure_reason"), lvs.get("reason")),
    }


def _classify(artifacts: dict[str, dict[str, Any]], category_counts: dict[str, int]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    sta = _sta_metrics(artifacts)

    analog = artifacts.get("analog_signoff") or {}
    analog_package = artifacts.get("analog_physical_package") or {}
    if (
        analog_package.get("blackbox_for_drc_lvs")
        or analog_package.get("blackbox_reason") == "analog_macro_gds_missing"
        or analog_package.get("reason") == "analog_macro_gds_missing"
    ):
        issues.append({
            "type": "analog_macro_gds_missing",
            "severity": 96,
            "restart_stage": "Analog Physical Collateral Package Agent",
            "evidence": {
                "status": analog_package.get("status"),
                "reason": _first_present(analog_package.get("blackbox_reason"), analog_package.get("reason")),
                "gds": analog_package.get("gds"),
                "mode": analog_package.get("mode"),
            },
            "reason": "Analog macro LEF/LIB are present but GDS is missing, so digital DRC/LVS cannot be trusted.",
        })
    analog_drc = analog.get("drc") if isinstance(analog.get("drc"), dict) else {}
    analog_lvs = analog.get("lvs") if isinstance(analog.get("lvs"), dict) else {}
    if analog_drc and not _status_clean(analog_drc.get("status"), {"clean", "ok", "pass", "not_applicable"}):
        issues.append({
            "type": "analog_drc",
            "severity": 95,
            "restart_stage": "Analog Physical Collateral Package Agent",
            "evidence": {"status": analog_drc.get("status"), "issues": analog_drc.get("feedback_problem_count")},
        })
    if analog_lvs and not _status_clean(analog_lvs.get("status"), {"clean", "ok", "pass", "not_applicable"}):
        issues.append({
            "type": "analog_lvs",
            "severity": 94,
            "restart_stage": "Analog Physical Collateral Package Agent",
            "evidence": {"status": analog_lvs.get("status")},
        })

    drc = artifacts.get("drc") or {}
    drc_count = _first_number(drc.get("drc_violations"), drc.get("violations"))
    drc_status = str(_first_present(drc.get("drc_status"), drc.get("status"), "")).lower()
    if (drc_count is not None and drc_count > 0) or drc_status in {"violations_found", "failed"}:
        stage, reason = _drc_restart_stage(category_counts)
        issues.append({
            "type": "digital_drc",
            "severity": 90,
            "restart_stage": stage,
            "evidence": {"status": drc_status, "violations": drc_count, "top_categories": category_counts},
            "reason": reason,
        })

    lvs = artifacts.get("lvs") or {}
    lvs_status = _known_status(lvs.get("lvs_status"), lvs.get("status"))
    if lvs_status and not _status_clean(lvs_status, {"clean", "ok", "pass", "completed"}):
        reason = str(_first_present(lvs.get("failure_reason"), lvs.get("reason"), "")).lower()
        if not (lvs_status == "blackbox_deferred" and "analog_macro_gds_missing" in reason):
            stage = "Digital Floorplan Agent" if "pin" in reason or lvs_status == "mismatch" else "Digital LVS Agent"
            issues.append({
                "type": "digital_lvs",
                "severity": 85,
                "restart_stage": stage,
                "evidence": {"status": lvs_status, "reason": reason},
            })

    if (sta.get("hold_violations") or 0) > 0:
        issues.append({
            "type": "hold_timing",
            "severity": 80,
            "restart_stage": "Digital CTS Agent",
            "evidence": sta,
            "reason": "Post-fill hold violations normally require CTS/buffer ECO before rerunning route/fill/signoff.",
        })
    elif (sta.get("setup_violations") or 0) > 0 or (sta.get("wns") is not None and sta["wns"] < 0):
        issues.append({
            "type": "setup_timing",
            "severity": 75,
            "restart_stage": "Digital Placement Agent",
            "evidence": sta,
            "reason": "Post-fill setup violations normally require placement/sizing/route timing ECO.",
        })

    lec = artifacts.get("lec") or {}
    lec_status = _known_status(lec.get("status"), lec.get("lec_status"))
    if lec_status and lec_status not in {"pass", "ok", "clean"}:
        reason = str(_first_present(lec.get("failure_reason"), lec.get("reason"), "")).lower()
        stage = "Digital Synthesis Agent" if re.search(r"missing|unresolved|model|parse|read_verilog", reason) else "Digital Logic Equivalence Check Agent"
        issues.append({
            "type": "synthesis_lec",
            "severity": 70,
            "restart_stage": stage,
            "evidence": {"status": lec_status, "reason": reason, "unproven_points": lec.get("unproven_points")},
        })

    tapeout = artifacts.get("tapeout") or {}
    tapeout_lec = artifacts.get("tapeout_lec") or {}
    tapeout_status = _known_status(tapeout.get("status"))
    tapeout_lec_status = _known_status(tapeout_lec.get("status"), tapeout_lec.get("lec_status"))
    tapeout_lec_reason = str(_first_present(tapeout_lec.get("failure_reason"), tapeout_lec.get("reason"), "")).lower()
    if tapeout_lec_status and tapeout_lec_status not in {"pass", "ok", "clean"}:
        if tapeout_lec_status == "blocked" or "blocked_by_tapeout_failure" in tapeout_lec_reason or tapeout_status == "failed":
            issues.append({
                "type": "tapeout_lec_blocked",
                "severity": 20,
                "restart_stage": "Digital Tapeout Agent",
                "evidence": {"status": tapeout_lec_status, "reason": tapeout_lec_reason},
                "blocked_by_upstream_signoff": True,
            })
        else:
            issues.append({
                "type": "tapeout_lec",
                "severity": 65,
                "restart_stage": "Digital Tapeout Logic Equivalence Check Agent",
                "evidence": {
                    "status": tapeout_lec_status,
                    "reason": tapeout_lec_reason,
                    "unproven_points": tapeout_lec.get("unproven_points"),
                },
            })

    issues.sort(key=lambda item: (-int(item.get("severity", 0)), STAGE_RANK.get(str(item.get("restart_stage")), 999)))
    return issues, sta


def _select_restart_stage(issues: list[dict[str, Any]]) -> str | None:
    actionable = [
        issue for issue in issues
        if not issue.get("blocked_by_upstream_signoff")
        and str(issue.get("restart_stage")) in STAGE_RANK
        and (
            str(issue.get("restart_stage")).startswith("Analog ")
            or STAGE_RANK.get(str(issue.get("restart_stage")), 999) >= PD_RESTART_MIN_RANK
        )
    ]
    if not actionable:
        return None
    return min(
        (str(issue.get("restart_stage")) for issue in actionable if issue.get("restart_stage")),
        key=lambda stage: STAGE_RANK.get(stage, 999),
        default=None,
    )


def _stage_config(workflow_dir: str, stage: str) -> dict[str, Any]:
    return _read_json(os.path.join(workflow_dir, "digital", stage, "config.json"))


def _die_area_scaled(value: Any, scale: float) -> str | None:
    try:
        parts = [float(x) for x in str(value or "").split()]
    except Exception:
        return None
    if len(parts) != 4:
        return None
    llx, lly, urx, ury = parts
    cx = (llx + urx) / 2.0
    cy = (lly + ury) / 2.0
    half_w = max((urx - llx) * scale / 2.0, 1.0)
    half_h = max((ury - lly) * scale / 2.0, 1.0)
    return f"{cx - half_w:g} {cy - half_h:g} {cx + half_w:g} {cy + half_h:g}"


def _eco_profile(
    state: dict[str, Any],
    workflow_dir: str,
    *,
    issues: list[dict[str, Any]],
    category_counts: dict[str, int],
    sta: dict[str, Any],
    closure_complete: bool,
) -> dict[str, Any]:
    if closure_complete:
        return {"enabled": False, "reason": "signoff_clean", "config_overrides": {}, "notes": []}

    raw_iteration = _first_present(state.get("signoff_closure_iteration_index"), state.get("closure_iteration_index"), 1)
    try:
        iteration = max(1, int(raw_iteration))
    except Exception:
        iteration = 1
    total_drc = sum(int(v or 0) for v in category_counts.values())
    severe_drc = total_drc >= 100
    severity_scale = 1.30 if (iteration <= 1 and severe_drc) else (1.15 if iteration <= 1 else 1.30)
    names = " ".join(category_counts.keys()).lower()

    floorplan_cfg = _stage_config(workflow_dir, "floorplan") or _stage_config(workflow_dir, "impl_setup")
    route_cfg = _stage_config(workflow_dir, "route")

    overrides: dict[str, dict[str, Any]] = {}
    notes: list[str] = []

    floorplan_overrides: dict[str, Any] = {}
    if re.search(r"\b(nwell|pwell|tap|difftap|metaltap|macro|boundary|obs|obstruction|pdn|power)\b", names):
        util = _first_number(floorplan_cfg.get("FP_CORE_UTIL"), floorplan_cfg.get("CORE_UTILIZATION"))
        if util is not None:
            util_factor = 0.72 if (iteration <= 1 and severe_drc) else (0.85 if iteration <= 1 else 0.75)
            floorplan_overrides["FP_CORE_UTIL"] = max(5, round(float(util) * util_factor, 3))
        density = _first_number(
            floorplan_cfg.get("PL_TARGET_DENSITY"),
            floorplan_cfg.get("PLACE_DENSITY"),
        )
        if density is not None:
            density_factor = 0.72 if (iteration <= 1 and severe_drc) else (0.85 if iteration <= 1 else 0.75)
            floorplan_overrides["PL_TARGET_DENSITY"] = max(0.05, round(float(density) * density_factor, 4))
            floorplan_overrides["PL_TARGET_DENSITY_PCT"] = round(floorplan_overrides["PL_TARGET_DENSITY"] * 100.0, 3)
        die_area = _die_area_scaled(floorplan_cfg.get("DIE_AREA"), severity_scale)
        if die_area:
            floorplan_overrides["DIE_AREA"] = die_area
        if re.search(r"\b(difftap|tap|metaltap)\b", names):
            base_tap_dist = _first_number(floorplan_cfg.get("FP_TAPCELL_DIST"), 14)
            tap_factor = 0.60 if (iteration <= 1 and severe_drc) else (0.75 if iteration <= 1 else 0.60)
            floorplan_overrides["FP_TAPCELL_DIST"] = max(6, round(float(base_tap_dist) * tap_factor, 3))
        if re.search(r"\b(macro|boundary|obs|obstruction|pdn|power|nwell|pwell|difftap)\b", names):
            halo = 12 if (iteration <= 1 and severe_drc) else (8 if iteration <= 1 else 12)
            floorplan_overrides["FP_PDN_HORIZONTAL_HALO"] = max(float(_first_number(floorplan_cfg.get("FP_PDN_HORIZONTAL_HALO"), 0) or 0), halo)
            floorplan_overrides["FP_PDN_VERTICAL_HALO"] = max(float(_first_number(floorplan_cfg.get("FP_PDN_VERTICAL_HALO"), 0) or 0), halo)
            floorplan_overrides["PL_MACRO_HALO"] = max(float(_first_number(floorplan_cfg.get("PL_MACRO_HALO"), 0) or 0), halo)
        floorplan_overrides["CHIPLOOP_SIGNOFF_CLOSURE_ECO"] = f"drc_floorplan_spacing_iter_{iteration}"
        notes.append("DRC includes tap/well/macro/PDN-like categories; apply lower utilization/density and more die area before placement.")

    route_overrides: dict[str, Any] = {}
    if re.search(r"\b(poly|licon|mcon|contact|li|via|met[1-5]|m[1-5])\b", names):
        route_overrides["RUN_SPEF_EXTRACTION"] = True
        route_overrides["CHIPLOOP_SIGNOFF_CLOSURE_ECO"] = f"drc_route_contact_metal_iter_{iteration}"
        notes.append("DRC includes local contact/metal categories; use floorplan/placement spacing first and avoid unsafe detailed-route override knobs.")

    setup_vios = sta.get("setup_violations") or 0
    hold_vios = sta.get("hold_violations") or 0
    if setup_vios or (sta.get("setup_wns") is not None and sta.get("setup_wns") < 0):
        route_overrides["RUN_SPEF_EXTRACTION"] = True
        route_overrides["CHIPLOOP_TIMING_CLOSURE_ECO"] = f"setup_iter_{iteration}"
        notes.append("Post-fill setup is negative; preserve SPEF and rerun downstream timing with closure profile.")
    if hold_vios or (sta.get("hold_wns") is not None and sta.get("hold_wns") < 0):
        route_overrides["RUN_SPEF_EXTRACTION"] = True
        route_overrides["CHIPLOOP_TIMING_CLOSURE_ECO"] = f"hold_iter_{iteration}"
        notes.append("Post-fill hold is negative; CTS/route must be rerun before accepting signoff.")

    if floorplan_overrides:
        overrides["floorplan"] = floorplan_overrides
        overrides["placement"] = dict(floorplan_overrides)
    if route_overrides:
        overrides["route"] = route_overrides
        fill_overrides = {"RUN_FILL_INSERTION": True}
        if re.search(r"\b(licon|difftap|li|m1)\b", names):
            fill_overrides["CHIPLOOP_FILL_DRC_REPAIR"] = f"tap_contact_fill_spacing_iter_{iteration}"
        overrides["fill"] = fill_overrides
        overrides["sta_postfill"] = dict(route_overrides)
        overrides["drc"] = dict(route_overrides)
        overrides["lvs"] = dict(route_overrides)
        overrides["tapeout"] = dict(route_overrides)

    analog_issues = [issue for issue in issues if str(issue.get("type")) in {"analog_drc", "analog_lvs"}]
    if analog_issues:
        notes.append("Analog signoff has an analog-local issue; rerun analog physical collateral before trusting digital macro signoff.")

    return {
        "enabled": bool(overrides),
        "iteration": iteration,
        "strategy": "evidence_driven_tool_eco",
        "config_overrides": overrides,
        "notes": notes,
    }


def _build_plan(state: dict[str, Any], artifacts: dict[str, dict[str, Any]], category_counts: dict[str, int], agent_name: str, workflow_dir: str) -> dict[str, Any]:
    issues, sta = _classify(artifacts, category_counts)
    drc_info = _drc_metrics(artifacts)
    lvs_info = _lvs_metrics(artifacts)
    restart_stage = _select_restart_stage(issues)
    restart_rank = STAGE_RANK.get(restart_stage or "", None)
    options = {
        "timing": _repair_enabled(state, "allow_timing_repair"),
        "drc": _repair_enabled(state, "allow_drc_repair"),
        "lvs": _repair_enabled(state, "allow_lvs_repair"),
        "lec": _repair_enabled(state, "allow_lec_repair"),
    }

    repair_actions = []
    skipped_repairs = []
    for issue in issues:
        issue_type = str(issue.get("type") or "")
        if issue.get("blocked_by_upstream_signoff"):
            skipped_repairs.append({
                "type": issue_type,
                "reason": "Blocked by earlier signoff failure; repair the upstream physical issue first.",
            })
            continue
        issue_stage = str(issue.get("restart_stage") or "")
        if issue_stage not in STAGE_RANK or (
            not issue_stage.startswith("Analog ")
            and STAGE_RANK.get(issue_stage, 999) < PD_RESTART_MIN_RANK
        ):
            skipped_repairs.append({
                "type": issue_type,
                "reason": "Outside PD signoff closure scope; rerun synthesis closure or synthesis LEC repair first.",
            })
            continue
        repair_group = (
            "timing" if issue_type in {"hold_timing", "setup_timing"}
            else "drc" if issue_type in {"digital_drc", "analog_drc"}
            else "lvs" if issue_type in {"digital_lvs", "analog_lvs", "analog_macro_gds_missing"}
            else "lec" if issue_type in {"synthesis_lec", "tapeout_lec"}
            else "other"
        )
        if repair_group in options and not options[repair_group]:
            skipped_repairs.append({"type": issue_type, "reason": f"{repair_group}_repair_disabled"})
            continue
        if restart_stage and STAGE_RANK.get(str(issue.get("restart_stage")), 999) > STAGE_RANK.get(restart_stage, 999):
            skipped_repairs.append({
                "type": issue_type,
                "reason": f"Selected restart at {restart_stage} invalidates later-stage ECO; re-evaluate after rerun.",
            })
            continue
        repair_actions.append({
            "type": issue_type,
            "restart_stage": issue.get("restart_stage"),
            "action": _repair_action(issue_type),
            "evidence": issue.get("evidence") or {},
            "reason": issue.get("reason"),
        })

    closure_complete = not [issue for issue in issues if not issue.get("blocked_by_upstream_signoff")]
    if restart_stage is None and any(issue.get("blocked_by_upstream_signoff") for issue in issues):
        closure_complete = False
    eco = _eco_profile(
        state,
        workflow_dir,
        issues=issues,
        category_counts=category_counts,
        sta=sta,
        closure_complete=closure_complete,
    )

    return {
        "type": "system_pd_signoff_closure_plan",
        "agent": agent_name,
        "enabled": True,
        "max_iterations": _max_iterations(state),
        "iterations_planned": 0 if closure_complete else 1,
        "closure_complete": closure_complete,
        "stop_reason": "signoff_clean" if closure_complete else "repair_plan_created",
        "selected_restart_stage": restart_stage,
        "selected_restart_stage_rank": restart_rank,
        "dominant_issue": issues[0]["type"] if issues else None,
        "repair_options": options,
        "postfill_timing": sta,
        "digital_drc": drc_info,
        "digital_lvs": lvs_info,
        "issue_summary": issues,
        "repair_actions": repair_actions,
        "eco_profile": eco,
        "skipped_repairs": skipped_repairs,
        "policy": {
            "bounded_iterations": "1-2",
            "lec_repair_included": options["lec"],
            "no_fake_closure": True,
            "restart_rule": "Choose the earliest stage required by the dominant real signoff failure; later ECOs are skipped until rerun evidence is available.",
            "pd_scope_only": True,
            "earliest_pd_restart_stage": "Digital STA PrePlace Agent",
        },
    }


def _repair_action(issue_type: str) -> str:
    return {
        "analog_drc": "Regenerate analog physical collateral, then rerun LEF/LIB consistency and digital physical signoff.",
        "analog_lvs": "Repair analog SPICE/GDS pin and device correspondence, then rerun analog LVS and downstream digital signoff.",
        "analog_macro_gds_missing": "Generate real analog macro GDS/SPICE collateral, then rerun digital DRC/LVS/tapeout signoff.",
        "digital_drc": "Classify DRC categories, apply the corresponding floorplan/route/fill ECO, then rerun from the selected physical stage.",
        "digital_lvs": "Repair extracted-vs-source netlist or pin/cell mismatch, then rerun LVS and tapeout signoff.",
        "hold_timing": "Apply CTS/buffer hold ECO, then rerun route, fill, post-fill STA, DRC, LVS, tapeout, and LEC.",
        "setup_timing": "Apply placement/sizing/routing timing ECO, then rerun later physical signoff.",
        "synthesis_lec": "Repair synthesis LEC setup or synthesis netlist/model mismatch before physical signoff is trusted.",
        "tapeout_lec": "Repair final gate-vs-reference LEC after tapeout artifacts are otherwise clean.",
    }.get(issue_type, "Inspect issue evidence and rerun the required signoff stage.")


def _chart_from_plan(state: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    prior = state.get("signoff_closure_chart") if isinstance(state.get("signoff_closure_chart"), dict) else {}
    prior_series = prior.get("series") if isinstance(prior.get("series"), list) else []
    raw_iteration = _first_present(state.get("signoff_closure_iteration_index"), state.get("closure_iteration_index"), 0)
    try:
        iteration = max(0, int(raw_iteration))
    except Exception:
        iteration = 0
    timing = plan.get("postfill_timing") if isinstance(plan.get("postfill_timing"), dict) else {}
    issues = [item for item in (plan.get("issue_summary") or []) if isinstance(item, dict)]

    def _evidence(issue_type: str) -> dict[str, Any]:
        issue = next((item for item in issues if item.get("type") == issue_type), {})
        evidence = issue.get("evidence") if isinstance(issue.get("evidence"), dict) else {}
        return evidence

    drc = _evidence("digital_drc")
    lvs = _evidence("digital_lvs")
    drc_info = plan.get("digital_drc") if isinstance(plan.get("digital_drc"), dict) else {}
    lvs_info = plan.get("digital_lvs") if isinstance(plan.get("digital_lvs"), dict) else {}
    tapeout_lec = _evidence("tapeout_lec") or _evidence("tapeout_lec_blocked")
    analog_drc = _evidence("analog_drc")
    analog_lvs = _evidence("analog_lvs")
    point = {
        "iteration": iteration,
        "label": "baseline" if iteration == 0 else f"iteration {iteration}",
        "postfill_wns": timing.get("wns"),
        "postfill_tns": timing.get("tns"),
        "postfill_setup_wns": timing.get("setup_wns"),
        "postfill_setup_tns": timing.get("setup_tns"),
        "postfill_hold_wns": timing.get("hold_wns"),
        "postfill_hold_tns": timing.get("hold_tns"),
        "postfill_setup_violations": timing.get("setup_violations"),
        "postfill_hold_violations": timing.get("hold_violations"),
        "drc_violations": _first_present(drc.get("violations"), drc_info.get("violations")),
        "drc_status": _first_present(drc.get("status"), drc_info.get("status"), "clean" if not drc else None),
        "lvs_status": _first_present(lvs.get("status"), lvs_info.get("status"), "clean" if not lvs else None),
        "tapeout_lec_status": tapeout_lec.get("status"),
        "tapeout_lec_unproven_points": tapeout_lec.get("unproven_points"),
        "analog_drc_status": analog_drc.get("status"),
        "analog_drc_issues": analog_drc.get("issues"),
        "analog_lvs_status": analog_lvs.get("status"),
        "dominant_issue": plan.get("dominant_issue"),
        "selected_restart_stage": plan.get("selected_restart_stage"),
    }
    if prior_series:
        series = [item for item in prior_series if not (isinstance(item, dict) and item.get("iteration") == iteration)]
        series.append(point)
        series.sort(key=lambda item: int(item.get("iteration") or 0) if isinstance(item, dict) else 0)
    else:
        series = [point]
    return {
        "type": "pd_signoff_closure_chart",
        "metric_scope": "postfill_signoff",
        "series": series,
        "baseline_only": len(series) == 1,
        "no_fake_iterations": True,
    }


def _markdown(plan: dict[str, Any]) -> str:
    lines = [
        "# System PD Signoff Closure Plan",
        "",
        f"- closure_complete: `{plan['closure_complete']}`",
        f"- selected_restart_stage: `{plan.get('selected_restart_stage')}`",
        f"- dominant_issue: `{plan.get('dominant_issue')}`",
        f"- max_iterations: `{plan.get('max_iterations')}`",
        f"- stop_reason: `{plan.get('stop_reason')}`",
        "",
        "## Post-Fill Timing",
    ]
    timing = plan.get("postfill_timing") or {}
    lines.extend([
        f"- setup WNS: `{timing.get('setup_wns')}`",
        f"- setup TNS: `{timing.get('setup_tns')}`",
        f"- setup violations: `{timing.get('setup_violations')}`",
        f"- hold WNS: `{timing.get('hold_wns')}`",
        f"- hold TNS: `{timing.get('hold_tns')}`",
        f"- hold violations: `{timing.get('hold_violations')}`",
        "",
        "## Repair Actions",
    ])
    actions = plan.get("repair_actions") or []
    if not actions:
        lines.append("- none")
    for action in actions:
        lines.append(f"- `{action.get('type')}` from `{action.get('restart_stage')}`: {action.get('action')}")
    lines.append("")
    lines.append("## Skipped Repairs")
    skipped = plan.get("skipped_repairs") or []
    if not skipped:
        lines.append("- none")
    for item in skipped:
        lines.append(f"- `{item.get('type')}`: {item.get('reason')}")
    return "\n".join(lines)


def run_with_agent_name(state: dict, agent_name: str) -> dict:
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = os.path.abspath(state.get("workflow_dir") or f"backend/workflows/{workflow_id}")
    out_dir = os.path.join(workflow_dir, OUT_DIR)
    os.makedirs(out_dir, exist_ok=True)

    if not _enabled(state):
        plan = {
            "type": "system_pd_signoff_closure_plan",
            "agent": agent_name,
            "enabled": False,
            "status": "skipped",
            "reason": "signoff_closure_loop_not_enabled",
        }
    else:
        artifacts = _load_artifacts(workflow_dir)
        category_counts = _drc_category_counts(workflow_dir)
        plan = _build_plan(state, artifacts, category_counts, agent_name, workflow_dir)
        plan["status"] = "clean" if plan.get("closure_complete") else "planned"
    chart = _chart_from_plan(state, plan) if plan.get("enabled") else {
        "type": "pd_signoff_closure_chart",
        "series": [],
        "status": "skipped",
        "reason": plan.get("reason"),
    }

    json_text = json.dumps(plan, indent=2)
    chart_text = json.dumps(chart, indent=2)
    md_text = _markdown(plan) if plan.get("enabled") else "# System PD Signoff Closure Plan\n\n- status: `skipped`\n"
    json_path = os.path.join(out_dir, "signoff_closure_plan.json")
    md_path = os.path.join(out_dir, "signoff_closure_plan.md")
    with open(json_path, "w", encoding="utf-8") as f:
        f.write(json_text)
    with open(os.path.join(out_dir, "signoff_closure_chart.json"), "w", encoding="utf-8") as f:
        f.write(chart_text)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_text)

    try:
        save_text_artifact_and_record(workflow_id, agent_name, "digital", "signoff_closure/signoff_closure_plan.json", json_text)
        save_text_artifact_and_record(workflow_id, agent_name, "digital", "signoff_closure/signoff_closure_chart.json", chart_text)
        save_text_artifact_and_record(workflow_id, agent_name, "digital", "signoff_closure/signoff_closure_plan.md", md_text)
    except Exception as exc:
        print(f"{agent_name}: artifact upload failed: {exc}")

    state.setdefault("digital", {})["signoff_closure"] = {
        "status": plan.get("status"),
        "plan": plan,
        "chart": chart,
        "paths": {"json": json_path, "md": md_path},
    }
    state["signoff_closure_plan"] = plan
    state["signoff_closure_chart"] = chart
    return state


def run_agent(state: dict) -> dict:
    return run_with_agent_name(state, AGENT_NAME)

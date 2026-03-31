
import json
import logging
import os
import re
from typing import Optional

from ._embedded_common import ensure_workflow_dir, write_artifact

logger = logging.getLogger(__name__)

AGENT_NAME = "Embedded Register Validation Agent"
PHASE = "reg_validate"
OUTPUT_PATH = "firmware/hal/register_validation.md"
JSON_OUTPUT_PATH = "firmware/hal/register_validation.json"
DEBUG_PATH = "firmware/hal/register_validation_debug.json"
MANIFEST_PATH = "firmware/firmware_manifest.json"


def _safe_load_json(path: str) -> Optional[dict]:
    try:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as exc:
        logger.warning("%s failed loading %s: %s", AGENT_NAME, path, exc)
    return None


def _safe_read(path: str) -> str:
    try:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
    except Exception as exc:
        logger.warning("%s failed reading %s: %s", AGENT_NAME, path, exc)
    return ""


def _safe_identifier(name: str) -> str:
    ident = re.sub(r"[^A-Za-z0-9_]", "_", name or "")
    if not ident:
        ident = "unnamed"
    if ident[0].isdigit():
        ident = f"r_{ident}"
    return ident.lower()


def _safe_const_name(name: str) -> str:
    ident = re.sub(r"[^A-Za-z0-9_]", "_", name or "")
    if not ident:
        ident = "UNNAMED"
    if ident[0].isdigit():
        ident = f"R_{ident}"
    return ident.upper()


def _load_manifest(state: dict, workflow_dir: str) -> dict:
    manifest = state.get("firmware_manifest") or (state.get("firmware") or {}).get("manifest")
    if isinstance(manifest, dict):
        return dict(manifest)
    manifest_path = state.get("firmware_manifest_path") or (state.get("firmware") or {}).get("manifest_path") or MANIFEST_PATH
    if manifest_path and not os.path.isabs(manifest_path):
        manifest_path = os.path.join(workflow_dir, manifest_path)
    loaded = _safe_load_json(manifest_path)
    return loaded if isinstance(loaded, dict) else {}


def _load_regmap(state: dict, workflow_dir: str, manifest: dict) -> dict:
    regmap = state.get("firmware_register_map") or (state.get("firmware") or {}).get("register_map")
    if isinstance(regmap, dict):
        return regmap
    regmap_path = state.get("firmware_register_map_path") or manifest.get("register_map_path") or "firmware/register_map.json"
    if regmap_path and not os.path.isabs(regmap_path):
        regmap_path = os.path.join(workflow_dir, regmap_path)
    loaded = _safe_load_json(regmap_path)
    return loaded if isinstance(loaded, dict) else {}


def _load_hal_code(state: dict, workflow_dir: str, manifest: dict) -> str:
    hal_code = state.get("firmware_hal_code") or (state.get("firmware") or {}).get("hal_code")
    if hal_code:
        return hal_code
    hal_path = state.get("firmware_hal_path") or manifest.get("hal_path") or "firmware/hal/registers.rs"
    if hal_path and not os.path.isabs(hal_path):
        hal_path = os.path.join(workflow_dir, hal_path)
    return _safe_read(hal_path)


def _load_driver_code(state: dict, workflow_dir: str, manifest: dict) -> str:
    driver_code = state.get("firmware_driver_code") or (state.get("firmware") or {}).get("driver_code")
    if driver_code:
        return driver_code
    driver_path = state.get("firmware_driver_path") or manifest.get("driver_path") or "firmware/drivers/driver_scaffold.rs"
    if driver_path and not os.path.isabs(driver_path):
        driver_path = os.path.join(workflow_dir, driver_path)
    return _safe_read(driver_path)


def _validate(regmap: dict, hal_code: str, driver_code: str) -> dict:
    findings = []
    regs = regmap.get("registers") or []

    for reg in regs:
        reg_name = reg.get("name") or "UNNAMED"
        reg_ident = _safe_identifier(reg_name)
        reg_const = f"{_safe_const_name(reg_name)}_OFFSET"
        reg_access = str(reg.get("access") or "RW").upper()

        if reg_const not in hal_code:
            findings.append({"severity": "error", "category": "hal", "message": f"Missing HAL constant {reg_const}"})
        if f"read_{reg_ident}" not in hal_code:
            findings.append({"severity": "error", "category": "hal", "message": f"Missing HAL read helper read_{reg_ident}"})
        if reg_access not in {"RO"} and f"write_{reg_ident}" not in hal_code:
            findings.append({"severity": "error", "category": "hal", "message": f"Missing HAL write helper write_{reg_ident}"})
        if reg_access in {"RO"} and f"write_{reg_ident}" in hal_code:
            findings.append({"severity": "warning", "category": "hal", "message": f"RO register {reg_name} exposes HAL write helper write_{reg_ident}"})

        if driver_code:
            if f"read_{reg_ident}" not in driver_code:
                findings.append({"severity": "error", "category": "driver", "message": f"Missing driver read helper read_{reg_ident}"})
            if reg_access not in {"RO"} and f"write_{reg_ident}" not in driver_code:
                findings.append({"severity": "error", "category": "driver", "message": f"Missing driver write helper write_{reg_ident}"})

        for field in reg.get("fields") or []:
            field_name = field.get("name") or "UNNAMED_FIELD"
            field_ident = _safe_identifier(field_name)
            field_access = str(field.get("access") or reg_access).upper()

            hal_get = f"get_{reg_ident}_{field_ident}"
            if hal_get not in hal_code:
                findings.append({"severity": "error", "category": "hal", "message": f"Missing HAL field getter {hal_get}"})

            hal_set = f"set_{reg_ident}_{field_ident}"
            if field_access not in {"RO"} and hal_set not in hal_code:
                findings.append({"severity": "error", "category": "hal", "message": f"Missing HAL field setter {hal_set}"})
            if field_access in {"RO"} and hal_set in hal_code:
                findings.append({"severity": "warning", "category": "hal", "message": f"RO field {reg_name}.{field_name} exposes HAL field setter {hal_set}"})

            if driver_code:
                drv_get = f"get_{reg_ident}_{field_ident}"
                if drv_get not in driver_code:
                    findings.append({"severity": "error", "category": "driver", "message": f"Missing driver field getter {drv_get}"})

                drv_set = f"set_{reg_ident}_{field_ident}"
                if field_access not in {"RO"} and drv_set not in driver_code:
                    findings.append({"severity": "error", "category": "driver", "message": f"Missing driver field setter {drv_set}"})
                if field_access in {"RO"} and drv_set in driver_code:
                    findings.append({"severity": "warning", "category": "driver", "message": f"RO field {reg_name}.{field_name} exposes driver field setter {drv_set}"})

    errors = [f for f in findings if f["severity"] == "error"]
    warnings = [f for f in findings if f["severity"] == "warning"]
    return {
        "agent": AGENT_NAME,
        "phase": PHASE,
        "register_count": len(regs),
        "driver_present": bool(driver_code),
        "error_count": len(errors),
        "warning_count": len(warnings),
        "status": "pass" if not errors else "fail",
        "findings": findings,
    }


def _render_markdown(report: dict) -> str:
    lines = [
        "# Register / HAL / Driver Validation",
        "",
        f"- Status: **{report.get('status')}**",
        f"- Register count: **{report.get('register_count')}**",
        f"- Driver present: **{report.get('driver_present')}**",
        f"- Errors: **{report.get('error_count')}**",
        f"- Warnings: **{report.get('warning_count')}**",
        "",
        "## Findings",
        "",
    ]
    if not report.get("findings"):
        lines.append("- No findings.")
    else:
        for finding in report.get("findings") or []:
            lines.append(f"- {finding['severity'].upper()} [{finding['category']}]: {finding['message']}")
    lines.append("")
    return "\n".join(lines)


def run_agent(state: dict) -> dict:
    print(f"\n🚀 Running {AGENT_NAME}...")
    logger.info("Starting %s", AGENT_NAME)
    ensure_workflow_dir(state)

    workflow_dir = state.get("workflow_dir") or ""
    manifest = _load_manifest(state, workflow_dir)
    regmap_obj = _load_regmap(state, workflow_dir, manifest)
    hal_code = _load_hal_code(state, workflow_dir, manifest)
    driver_code = _load_driver_code(state, workflow_dir, manifest)

    if not regmap_obj:
        state["status"] = "❌ firmware register map missing in state for register validation"
        return state
    if not hal_code:
        state["status"] = "❌ firmware HAL missing in state for register validation"
        return state

    report = _validate(regmap_obj, hal_code, driver_code)
    write_artifact(state, JSON_OUTPUT_PATH, json.dumps(report, indent=2), key=os.path.basename(JSON_OUTPUT_PATH))
    write_artifact(state, OUTPUT_PATH, _render_markdown(report), key=os.path.basename(OUTPUT_PATH))
    write_artifact(
        state,
        DEBUG_PATH,
        json.dumps(
            {
                "agent": AGENT_NAME,
                "manifest_present": bool(manifest),
                "register_count": len(regmap_obj.get("registers") or []),
                "hal_length": len(hal_code),
                "driver_length": len(driver_code),
                "status": report.get("status"),
            },
            indent=2,
        ),
        key=os.path.basename(DEBUG_PATH),
    )

    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH

    state["register_validation_report"] = report
    state["status"] = f"✅ {AGENT_NAME} done ({report.get('status')})"
    return state

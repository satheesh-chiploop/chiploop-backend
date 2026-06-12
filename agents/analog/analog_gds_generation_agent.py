import json
import os
import shlex
import shutil
from datetime import datetime
from typing import Any, Dict

from tooling.runner import run_command
from utils.artifact_utils import save_text_artifact_and_record


AGENT_NAME = "Analog GDS Generation Agent"
ALIGN_DOCKER_IMAGE = "darpaalign/align-public:latest"


def _enabled(state: dict) -> bool:
    mode = str(state.get("analog_physical_mode") or "").strip().lower()
    pdk = str(state.get("analog_pdk") or state.get("pdk") or "").strip().lower()
    return mode in {"generate_sky130_gds", "sky130_gds"} or (mode == "generate_gds" and pdk.startswith("sky130"))


def _module_name(state: dict) -> str:
    contract = state.get("analog_macro_interface_contract") if isinstance(state.get("analog_macro_interface_contract"), dict) else {}
    return str(state.get("analog_macro_module") or contract.get("macro_name") or "analog_macro").strip()


def _find_gds(root: str) -> str:
    hits = []
    for dirpath, _, files in os.walk(root):
        for name in files:
            if name.lower().endswith(".gds"):
                hits.append(os.path.join(dirpath, name))
    hits.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return hits[0] if hits else ""


def _docker_available() -> bool:
    return bool(shutil.which("docker"))


def _tail(text: str, limit: int = 2000) -> str:
    text = text or ""
    return text[-limit:] if len(text) > limit else text


def _resolve_pdk_variant(state: dict) -> str:
    return str(
        state.get("pdk_variant")
        or state.get("analog_pdk")
        or state.get("pdk")
        or os.getenv("CHIPLOOP_PDK_VARIANT")
        or "sky130A"
    ).strip()


def _resolve_pdk_root_host(state: dict) -> str:
    pdk_root = (
        state.get("pdk_root_host")
        or os.getenv("CHIPLOOP_PDK_ROOT_HOST")
        or "/root/chiploop-backend/backend/pdk"
    )
    pdk_root = os.path.abspath(str(pdk_root))
    state["pdk_root_host"] = pdk_root
    return pdk_root


def _host_align_pdk_arg(state: dict, pdk_variant: str, pdk_root_host: str) -> str:
    candidates = [
        os.path.join(pdk_root_host, pdk_variant),
        os.path.join(pdk_root_host, "sky130"),
    ]
    for path in candidates:
        if os.path.isdir(path) and os.path.exists(os.path.join(path, "primitive.py")):
            return path
    return "sky130"


def _align_docker_script(spice_name: str, module_name: str, pdk_variant: str) -> str:
    return "\n".join([
        "set -eu",
        "PY_BIN=\"$(command -v python3 || command -v python)\"",
        "PDK_DIR=\"$(${PY_BIN} - <<'PY'",
        "from pathlib import Path",
        "import align",
        "import sys",
        f"variant = {pdk_variant!r}",
        "root = Path(align.__file__).resolve().parent",
        "candidates = [",
        "    Path('/pdk') / variant,",
        "    Path('/pdk/sky130'),",
        "    root / 'pdk' / 'sky130',",
        "    root / 'pdks' / 'sky130',",
        "    root.parent / 'pdks' / 'sky130',",
        "    Path('/ALIGN-public/pdks/sky130'),",
        "    Path('/align/pdk/sky130'),",
        "    Path('/pdks/sky130'),",
        "]",
        "for path in candidates:",
        "    if path.exists() and (path / 'primitive.py').exists():",
        "        print(path)",
        "        sys.exit(0)",
        "print('ALIGN Sky130 PDK directory with primitive.py not found in container or mounted /pdk', file=sys.stderr)",
        "sys.exit(2)",
        "PY",
        ")\"",
        "echo \"ALIGN_PDK_DIR=${PDK_DIR}\"",
        (
            "schematic2layout.py /work -p \"${PDK_DIR}\" "
            f"-f {shlex.quote(spice_name)} -s {shlex.quote(module_name)}"
        ),
    ])


def run_agent(state: dict) -> dict:
    print(f"\nRunning {AGENT_NAME}...")
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = os.path.abspath(state.get("workflow_dir") or f"backend/workflows/{workflow_id}")
    stage_dir = os.path.join(workflow_dir, "analog", "gds")
    os.makedirs(stage_dir, exist_ok=True)

    if not _enabled(state):
        state["analog_gds_generation"] = {"status": "skipped", "reason": "analog_physical_mode_not_generate_sky130_gds"}
        state["status"] = f"{AGENT_NAME}: skipped"
        return state

    module_name = _module_name(state)
    pdk_variant = _resolve_pdk_variant(state)
    pdk_root_host = _resolve_pdk_root_host(state)
    spice_path = state.get("analog_spice_path") or state.get("analog_netlist_path")
    summary: Dict[str, Any] = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "pdk": pdk_variant,
        "pdk_root_host": pdk_root_host,
        "module": module_name,
        "spice": spice_path,
    }

    if not isinstance(spice_path, str) or not os.path.exists(spice_path):
        summary.update({"status": "failed", "reason": "sky130_spice_missing"})
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "analog_gds_summary.json", json.dumps(summary, indent=2))
        state["analog_gds_generation"] = summary
        state["status"] = f"{AGENT_NAME}: failed"
        raise RuntimeError("Analog GDS generation requires a generated or provided Sky130 transistor-level SPICE netlist.")

    align_bin = shutil.which("schematic2layout.py") or shutil.which("align")
    docker_bin = shutil.which("docker")
    spice_base = os.path.basename(spice_path) or f"{module_name}.spice"
    spice_stem, spice_ext = os.path.splitext(spice_base)
    align_spice_name = spice_base if spice_ext.lower() in {".sp", ".cdl"} else f"{spice_stem or module_name}.sp"
    staged_spice = os.path.join(stage_dir, align_spice_name)
    if os.path.abspath(spice_path) != os.path.abspath(staged_spice):
        shutil.copy2(spice_path, staged_spice)
    run_sh = os.path.join(stage_dir, "run_align.sh")
    if align_bin:
        host_pdk_arg = _host_align_pdk_arg(state, pdk_variant, pdk_root_host)
        expected_cmd = f"{align_bin} {os.path.abspath(stage_dir)} -p {host_pdk_arg} -f {os.path.basename(staged_spice)} -s {module_name}"
    else:
        docker_script = _align_docker_script(os.path.basename(staged_spice), module_name, pdk_variant)
        expected_cmd = (
            f"docker run --rm -v {pdk_root_host}:/pdk -v {os.path.abspath(stage_dir)}:/work -w /work "
            f"{ALIGN_DOCKER_IMAGE} sh -lc {shlex.quote(docker_script)}"
        )
    run_text = "\n".join([
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        f'echo "ChipLoop {AGENT_NAME}"',
        f'echo "SPICE={staged_spice}"',
        f'echo "TOP={module_name}"',
        f'echo "PDK={pdk_variant}"',
        f'echo "PDK_ROOT_HOST={pdk_root_host}"',
        expected_cmd,
        "",
    ])
    with open(run_sh, "w", encoding="utf-8") as fh:
        fh.write(run_text)
    try:
        os.chmod(run_sh, 0o755)
    except Exception:
        pass

    if not align_bin and not docker_bin:
        summary.update({
            "status": "failed",
            "reason": "align_not_installed",
            "run_script": run_sh,
            "note": "No GDS was generated. Install ALIGN/schematic2layout.py on PATH or provide Docker with darpaalign/align-public:latest.",
        })
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "run_align.sh", run_text)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "analog_gds_summary.json", json.dumps(summary, indent=2))
        state["analog_gds_generation"] = summary
        state["status"] = f"{AGENT_NAME}: failed"
        raise RuntimeError("Analog GDS generation failed: ALIGN/schematic2layout.py is not installed and Docker is not available.")

    if align_bin:
        host_pdk_arg = _host_align_pdk_arg(state, pdk_variant, pdk_root_host)
        cmd = [
            align_bin,
            os.path.abspath(stage_dir),
            "-p",
            host_pdk_arg,
            "-f",
            os.path.basename(staged_spice),
            "-s",
            module_name,
        ]
        tool_mode = "host"
    else:
        docker_script = _align_docker_script(os.path.basename(staged_spice), module_name, pdk_variant)
        cmd = [
            docker_bin,
            "run",
            "--rm",
            "-v",
            f"{pdk_root_host}:/pdk",
            "-v",
            f"{os.path.abspath(stage_dir)}:/work",
            "-e",
            f"PDK={pdk_variant}",
            "-e",
            "PDK_ROOT=/pdk",
            "-w",
            "/work",
            ALIGN_DOCKER_IMAGE,
            "sh",
            "-lc",
            docker_script,
        ]
        tool_mode = "docker"
    cp = run_command(state, "analog_align_gds", cmd, cwd=stage_dir, timeout_sec=3600)
    log = (cp.stdout or "") + (cp.stderr or "")
    log_path = os.path.join(stage_dir, "align.log")
    with open(log_path, "w", encoding="utf-8", errors="ignore") as fh:
        fh.write(log)

    gds_path = _find_gds(stage_dir)
    if gds_path:
        final_gds = os.path.join(stage_dir, f"{module_name}.gds")
        if os.path.abspath(gds_path) != os.path.abspath(final_gds):
            shutil.copy2(gds_path, final_gds)
        summary.update({
            "status": "generated",
            "return_code": cp.returncode,
            "gds": final_gds,
            "log": log_path,
            "tool_mode": tool_mode,
            "image": ALIGN_DOCKER_IMAGE if tool_mode == "docker" else "",
        })
        with open(final_gds, "rb") as fh:
            # Store a small text breadcrumb instead of trying to upload binary through text helper.
            pass
        state["analog_macro_gds"] = final_gds
        digital = state.setdefault("digital", {})
        if isinstance(digital, dict):
            digital["macro_gds"] = list(dict.fromkeys((digital.get("macro_gds") or []) + [final_gds]))
            digital.pop("macro_blackbox_mode", None)
    else:
        summary.update({
            "status": "failed",
            "return_code": cp.returncode,
            "reason": "align_gds_not_produced",
            "log": log_path,
            "log_tail": _tail(log),
            "tool_mode": tool_mode,
            "image": ALIGN_DOCKER_IMAGE if tool_mode == "docker" else "",
        })

    save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "run_align.sh", run_text)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "align.log", log)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "analog_gds_summary.json", json.dumps(summary, indent=2))
    state["analog_gds_generation"] = summary
    state["status"] = f"{AGENT_NAME}: {summary['status']}"
    if summary["status"] != "generated":
        detail = summary.get("log_tail") or ""
        raise RuntimeError(
            f"Analog GDS generation failed: {summary.get('reason') or 'gds_not_produced'}"
            + (f"\nALIGN log tail:\n{detail}" if detail else "")
        )
    return state

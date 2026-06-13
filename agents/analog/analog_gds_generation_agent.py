import json
import os
import re
import shlex
import shutil
from datetime import datetime
from typing import Any, Dict

from tooling.runner import run_command
from utils.artifact_utils import save_text_artifact_and_record


AGENT_NAME = "Analog GDS Generation Agent"
ALIGN_DOCKER_IMAGE = "darpaalign/align-public:latest"
OPENLANE_DOCKER_IMAGE = os.getenv("CHIPLOOP_OPENLANE_IMAGE", "ghcr.io/efabless/openlane2:2.4.0.dev1")


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


def _prepare_magic_spice(src: str, dst: str) -> None:
    text = open(src, "r", encoding="utf-8", errors="ignore").read()

    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        value = match.group(2)
        suffix = match.group(3).lower()
        number = float(value)
        if suffix in {"u", "um"}:
            number = number
        elif suffix == "n":
            number = number / 1000.0
        elif suffix == "m":
            number = number * 1000.0
        if key.lower() == "w":
            number = max(number, 0.42)
        elif key.lower() == "l":
            number = max(number, 0.15)
        return f"{key}={number:g}"

    # Magic's Sky130 SPICE importer expects generator dimensions in microns and rejects
    # devices below Sky130 generator minima.
    text = re.sub(r"\b([WLwl])\s*=\s*([0-9]+(?:\.[0-9]+)?)(u|um|n|m)\b", repl, text)
    text = re.sub(
        r"\b([WLwl])\s*=\s*([0-9]+(?:\.[0-9]+)?)\b",
        lambda m: f"{m.group(1)}={max(float(m.group(2)), 0.42 if m.group(1).lower() == 'w' else 0.15):g}",
        text,
    )
    with open(dst, "w", encoding="utf-8") as fh:
        fh.write(text)


def _docker_available() -> bool:
    return bool(shutil.which("docker"))


def _tail(text: str, limit: int = 2000) -> str:
    text = text or ""
    return text[-limit:] if len(text) > limit else text


def _magic_layout_invalid(log: str) -> str:
    lowered = (log or "").lower()
    if "mos length must be" in lowered or "mos width must be" in lowered:
        return "magic_device_parameter_errors"
    if "generating output for cell /work/" in lowered or "cell /work/" in lowered:
        return "magic_path_qualified_top_cell"
    final_box = re.search(r"CHIPLOOP_FINAL_BOX=([^\n\r]*)", log or "")
    if final_box and re.search(r"\b0(?:\.0+)?\s+0(?:\.0+)?\s+0(?:\.0+)?\s+0(?:\.0+)?\b", final_box.group(1)):
        return "magic_zero_area_layout"
    if final_box and not final_box.group(1).strip():
        return "magic_placeholder_layout"
    return ""


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


def _resolve_align_pdk_host(state: dict) -> str:
    pdk_root = state.get("align_pdk_host") or os.getenv("CHIPLOOP_ALIGN_PDK_HOST") or ""
    pdk_root = os.path.abspath(str(pdk_root)) if pdk_root else ""
    if pdk_root:
        state["align_pdk_host"] = pdk_root
    return pdk_root


def _host_align_pdk_arg(state: dict, pdk_variant: str, pdk_root_host: str) -> str:
    candidates = [
        _resolve_align_pdk_host(state),
        os.path.join(pdk_root_host, pdk_variant),
        os.path.join(pdk_root_host, "sky130"),
    ]
    for path in candidates:
        if os.path.isdir(path) and os.path.exists(os.path.join(path, "primitive.py")):
            return path
    return "sky130"


def _gds_backend(state: dict) -> str:
    return str(state.get("analog_gds_backend") or os.getenv("CHIPLOOP_ANALOG_GDS_BACKEND") or "magic").strip().lower()


def _magic_paths(pdk_variant: str) -> tuple[str, str]:
    return (
        f"/pdk/{pdk_variant}/libs.tech/magic/{pdk_variant}.tech",
        f"/pdk/{pdk_variant}/libs.tech/magic/{pdk_variant}.tcl",
    )


def _host_magic_paths(pdk_root_host: str, pdk_variant: str) -> tuple[str, str]:
    return (
        os.path.join(pdk_root_host, pdk_variant, "libs.tech", "magic", f"{pdk_variant}.tech"),
        os.path.join(pdk_root_host, pdk_variant, "libs.tech", "magic", f"{pdk_variant}.tcl"),
    )


def _write_magic_import_tcl(
    stage_dir: str,
    spice_name: str,
    module_name: str,
    pdk_variant: str,
    pdk_root_host: str,
    use_docker: bool,
) -> str:
    tech_tcl = _magic_paths(pdk_variant)[1] if use_docker else _host_magic_paths(pdk_root_host, pdk_variant)[1]
    spice_path = spice_name
    gds_path = f"{module_name}.gds" if use_docker else os.path.join(stage_dir, f"{module_name}.gds")
    mag_path = f"{module_name}.mag" if use_docker else os.path.join(stage_dir, f"{module_name}.mag")
    feedback_path = "magic_feedback.txt" if use_docker else os.path.join(stage_dir, "magic_feedback.txt")
    lines = [
        "drc off",
        f"source {tech_tcl}",
        f"magic::netlist_to_layout {spice_path} sky130",
        f"load {module_name}",
        "select top cell",
        "expand",
        "puts stdout \"CHIPLOOP_FINAL_BOX=[box values]\"",
        f"catch {{feedback save {feedback_path}}}",
        f"save {mag_path}",
        f"gds write {gds_path}",
        "quit -noprompt",
        "",
    ]
    path = os.path.join(stage_dir, "magic_import_spice.tcl")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return path


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
        "    Path('/align_pdk'),",
        "    Path('/pdk') / variant,",
        "    Path('/pdk/sky130'),",
        "    root / 'pdk' / 'sky130',",
        "    root / 'pdks' / 'sky130',",
        "    root.parent / 'pdks' / 'sky130',",
        "    root.parent / 'pdk' / 'sky130',",
        "    root.parent.parent / 'pdks' / 'sky130',",
        "    root.parent.parent / 'pdk' / 'sky130',",
        "    Path('/ALIGN-public/pdks/sky130'),",
        "    Path('/ALIGN-public/pdk/sky130'),",
        "    Path('/ALIGN-public/align/pdk/sky130'),",
        "    Path('/align/pdk/sky130'),",
        "    Path('/align/pdks/sky130'),",
        "    Path('/pdks/sky130'),",
        "]",
        "for path in candidates:",
        "    if path.exists() and (path / 'primitive.py').exists():",
        "        print(path)",
        "        sys.exit(0)",
        "search_roots = [root, root.parent, root.parent.parent, Path('/ALIGN-public'), Path('/align'), Path('/usr/local/lib')]",
        "seen = set()",
        "for search_root in search_roots:",
        "    if not search_root.exists() or search_root in seen:",
        "        continue",
        "    seen.add(search_root)",
        "    for primitive in search_root.rglob('primitive.py'):",
        "        parent = primitive.parent",
        "        name = parent.name.lower()",
        "        if name in {'sky130', 'sky130a'} or 'sky130' in str(parent).lower():",
        "            print(parent)",
        "            sys.exit(0)",
        "print('ALIGN Sky130 PDK directory with primitive.py not found in mounted /align_pdk, mounted /pdk, or container image', file=sys.stderr)",
        "sys.exit(2)",
        "PY",
        ")\"",
        "echo \"ALIGN_PDK_DIR=${PDK_DIR}\"",
        (
            "schematic2layout.py /work -p \"${PDK_DIR}\" "
            f"-f {shlex.quote(spice_name)} -s {shlex.quote(module_name)}"
        ),
    ])


def _run_magic_gds(
    state: dict,
    stage_dir: str,
    staged_spice: str,
    module_name: str,
    pdk_variant: str,
    pdk_root_host: str,
    docker_bin: str | None,
) -> tuple[Any, str, str, str]:
    host_tech, host_tcl = _host_magic_paths(pdk_root_host, pdk_variant)
    if not os.path.exists(host_tech):
        raise RuntimeError(f"Magic GDS generation failed: missing Sky130 Magic tech file at {host_tech}")
    if not os.path.exists(host_tcl):
        raise RuntimeError(f"Magic GDS generation failed: missing Sky130 Magic Tcl file at {host_tcl}")

    magic_bin = shutil.which("magic")
    use_docker = not bool(magic_bin)
    tcl_path = _write_magic_import_tcl(
        stage_dir,
        os.path.basename(staged_spice),
        module_name,
        pdk_variant,
        pdk_root_host,
        use_docker,
    )
    if magic_bin:
        host_tcl_text = open(tcl_path, "r", encoding="utf-8").read()
        host_tcl_text = host_tcl_text.replace(f"/pdk/{pdk_variant}/libs.tech/magic/{pdk_variant}.tcl", host_tcl)
        with open(tcl_path, "w", encoding="utf-8") as fh:
            fh.write(host_tcl_text)
        cmd = [magic_bin, "-dnull", "-noconsole", "-T", host_tech, tcl_path]
        tool_mode = "host_magic"
        image = ""
    else:
        if not docker_bin:
            raise RuntimeError("Magic GDS generation failed: Magic is not installed and Docker is not available.")
        cmd = [
            docker_bin,
            "run",
            "--rm",
            "-v",
            f"{pdk_root_host}:/pdk",
            "-v",
            f"{os.path.abspath(stage_dir)}:/work",
            "-w",
            "/work",
            OPENLANE_DOCKER_IMAGE,
            "magic",
            "-dnull",
            "-noconsole",
            "-T",
            _magic_paths(pdk_variant)[0],
            "/work/magic_import_spice.tcl",
        ]
        tool_mode = "docker_magic"
        image = OPENLANE_DOCKER_IMAGE
    cp = run_command(state, "analog_magic_gds", cmd, cwd=stage_dir, timeout_sec=3600)
    return cp, tcl_path, tool_mode, image


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
    align_pdk_host = _resolve_align_pdk_host(state)
    backend = _gds_backend(state)
    spice_path = state.get("analog_spice_path") or state.get("analog_netlist_path")
    summary: Dict[str, Any] = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "pdk": pdk_variant,
        "pdk_root_host": pdk_root_host,
        "align_pdk_host": align_pdk_host,
        "backend": backend,
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
    if backend == "magic":
        _prepare_magic_spice(spice_path, staged_spice)
    elif os.path.abspath(spice_path) != os.path.abspath(staged_spice):
        shutil.copy2(spice_path, staged_spice)
    run_sh = os.path.join(stage_dir, "run_gds.sh")
    if backend == "magic":
        expected_cmd = (
            f"docker run --rm -v {pdk_root_host}:/pdk -v {os.path.abspath(stage_dir)}:/work -w /work "
            f"{OPENLANE_DOCKER_IMAGE} magic -dnull -noconsole -T /pdk/{pdk_variant}/libs.tech/magic/{pdk_variant}.tech "
            "/work/magic_import_spice.tcl"
        )
    elif align_bin:
        host_pdk_arg = _host_align_pdk_arg(state, pdk_variant, pdk_root_host)
        expected_cmd = f"{align_bin} {os.path.abspath(stage_dir)} -p {host_pdk_arg} -f {os.path.basename(staged_spice)} -s {module_name}"
    else:
        docker_script = _align_docker_script(os.path.basename(staged_spice), module_name, pdk_variant)
        align_pdk_mount = f"-v {align_pdk_host}:/align_pdk " if align_pdk_host else ""
        expected_cmd = (
            f"docker run --rm {align_pdk_mount}-v {pdk_root_host}:/pdk -v {os.path.abspath(stage_dir)}:/work -w /work "
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
        f'echo "ALIGN_PDK_HOST={align_pdk_host}"',
        expected_cmd,
        "",
    ])
    with open(run_sh, "w", encoding="utf-8") as fh:
        fh.write(run_text)
    try:
        os.chmod(run_sh, 0o755)
    except Exception:
        pass

    if backend not in {"magic", "align"}:
        summary.update({
            "status": "failed",
            "reason": "unsupported_gds_backend",
            "backend": backend,
        })
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "run_gds.sh", run_text)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "analog_gds_summary.json", json.dumps(summary, indent=2))
        state["analog_gds_generation"] = summary
        state["status"] = f"{AGENT_NAME}: failed"
        raise RuntimeError(f"Analog GDS generation failed: unsupported backend {backend}")

    if backend == "align" and not align_bin and not docker_bin:
        summary.update({
            "status": "failed",
            "reason": "align_not_installed",
            "run_script": run_sh,
            "note": "No GDS was generated. Install ALIGN/schematic2layout.py on PATH or provide Docker with darpaalign/align-public:latest.",
        })
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "run_gds.sh", run_text)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "analog_gds_summary.json", json.dumps(summary, indent=2))
        state["analog_gds_generation"] = summary
        state["status"] = f"{AGENT_NAME}: failed"
        raise RuntimeError("Analog GDS generation failed: ALIGN/schematic2layout.py is not installed and Docker is not available.")

    if backend == "magic":
        try:
            cp, tcl_path, tool_mode, image = _run_magic_gds(
                state, stage_dir, staged_spice, module_name, pdk_variant, pdk_root_host, docker_bin
            )
        except RuntimeError as exc:
            summary.update({"status": "failed", "reason": "magic_setup_failed", "error": str(exc)})
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "run_gds.sh", run_text)
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "analog_gds_summary.json", json.dumps(summary, indent=2))
            state["analog_gds_generation"] = summary
            state["status"] = f"{AGENT_NAME}: failed"
            raise
    elif align_bin:
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
        cp = run_command(state, "analog_align_gds", cmd, cwd=stage_dir, timeout_sec=3600)
        tcl_path = ""
        image = ""
    else:
        docker_script = _align_docker_script(os.path.basename(staged_spice), module_name, pdk_variant)
        cmd = [
            docker_bin,
            "run",
            "--rm",
        ]
        if align_pdk_host:
            cmd.extend(["-v", f"{align_pdk_host}:/align_pdk"])
        cmd.extend([
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
        ])
        tool_mode = "docker"
        cp = run_command(state, "analog_align_gds", cmd, cwd=stage_dir, timeout_sec=3600)
        tcl_path = ""
        image = ALIGN_DOCKER_IMAGE if tool_mode == "docker" else ""
    log = (cp.stdout or "") + (cp.stderr or "")
    log_name = "magic_gds.log" if backend == "magic" else "align.log"
    log_path = os.path.join(stage_dir, log_name)
    with open(log_path, "w", encoding="utf-8", errors="ignore") as fh:
        fh.write(log)

    gds_path = _find_gds(stage_dir)
    invalid_reason = _magic_layout_invalid(log) if backend == "magic" else ""
    if gds_path and not invalid_reason:
        final_gds = os.path.join(stage_dir, f"{module_name}.gds")
        if os.path.abspath(gds_path) != os.path.abspath(final_gds):
            shutil.copy2(gds_path, final_gds)
        summary.update({
            "status": "generated",
            "return_code": cp.returncode,
            "gds": final_gds,
            "log": log_path,
            "tool_mode": tool_mode,
            "image": image,
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
        reason = invalid_reason or ("magic_gds_not_produced" if backend == "magic" else "align_gds_not_produced")
        summary.update({
            "status": "failed",
            "return_code": cp.returncode,
            "reason": reason,
            "log": log_path,
            "log_tail": _tail(log),
            "tool_mode": tool_mode,
            "image": image,
        })

    save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "run_gds.sh", run_text)
    if backend == "magic" and tcl_path and os.path.exists(tcl_path):
        with open(tcl_path, "r", encoding="utf-8") as fh:
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "magic_import_spice.tcl", fh.read())
    if backend == "magic" and os.path.exists(staged_spice):
        with open(staged_spice, "r", encoding="utf-8", errors="ignore") as fh:
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "magic_input.sp", fh.read())
    feedback_path = os.path.join(stage_dir, "magic_feedback.txt")
    if backend == "magic" and os.path.exists(feedback_path):
        with open(feedback_path, "r", encoding="utf-8", errors="ignore") as fh:
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "magic_feedback.txt", fh.read())
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", log_name, log)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "analog_gds_summary.json", json.dumps(summary, indent=2))
    state["analog_gds_generation"] = summary
    state["status"] = f"{AGENT_NAME}: {summary['status']}"
    if summary["status"] != "generated":
        detail = summary.get("log_tail") or ""
        label = "Magic" if backend == "magic" else "ALIGN"
        raise RuntimeError(
            f"Analog GDS generation failed: {summary.get('reason') or 'gds_not_produced'}"
            + (f"\n{label} log tail:\n{detail}" if detail else "")
        )
    return state

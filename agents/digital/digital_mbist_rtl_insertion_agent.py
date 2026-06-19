import glob
import json
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from tooling.runner import tool_path
from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "Digital MBIST RTL Insertion Agent"


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _write_text(path: str, content: str) -> None:
    _ensure_dir(os.path.dirname(path))
    Path(path).write_text(content, encoding="utf-8")


def _read_text(path: str | None) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _run(cmd: list[str], cwd: str, timeout: int = 600) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout or ""
    except subprocess.TimeoutExpired as exc:
        return 124, (exc.stdout or "") + "\nTIMEOUT\n"
    except Exception as exc:
        return 1, f"{type(exc).__name__}: {exc}\n"


def _existing_path(value: Any, workflow_dir: str) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    candidates = [raw]
    if not os.path.isabs(raw):
        candidates.insert(0, os.path.join(workflow_dir, raw))
    for cand in candidates:
        try:
            path = os.path.abspath(cand)
            if os.path.isfile(path):
                return path
        except OSError:
            continue
    return None


def _rtl_files(state: dict, workflow_dir: str) -> list[str]:
    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    handoff = digital.get("handoff") if isinstance(digital.get("handoff"), dict) else {}
    candidates: list[str] = []
    for value in (
        state.get("rtl_files"),
        digital.get("rtl_files"),
        handoff.get("rtl_files"),
        handoff.get("imported_rtl_files"),
    ):
        if isinstance(value, list):
            candidates.extend(str(item) for item in value)
    found = [_existing_path(item, workflow_dir) for item in candidates]
    found = [item for item in found if item]
    if found:
        return sorted(dict.fromkeys(found))

    patterns = [
        os.path.join(workflow_dir, "rtl", "*.sv"),
        os.path.join(workflow_dir, "rtl", "*.v"),
        os.path.join(workflow_dir, "digital", "rtl", "*.sv"),
        os.path.join(workflow_dir, "digital", "rtl", "*.v"),
        os.path.join(workflow_dir, "handoff", "rtl", "*.sv"),
        os.path.join(workflow_dir, "handoff", "rtl", "*.v"),
        os.path.join(workflow_dir, "digital", "handoff", "rtl", "*.sv"),
        os.path.join(workflow_dir, "digital", "handoff", "rtl", "*.v"),
    ]
    hits: list[str] = []
    for pattern in patterns:
        hits.extend(glob.glob(pattern))
    return sorted(dict.fromkeys(os.path.abspath(path) for path in hits))


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"//.*", "", text)


def _parse_declaration_widths(text: str) -> dict[str, int]:
    widths: dict[str, int] = {}
    clean = _strip_comments(text)
    decl_re = re.compile(
        r"\b(?:input|output|inout|wire|logic|reg)\s+(?:wire\s+|logic\s+|reg\s+)?"
        r"(?P<range>\[[^\]]+\]\s+)?(?P<names>[^;]+);",
        re.IGNORECASE,
    )
    for match in decl_re.finditer(clean):
        width = _range_width(match.group("range"))
        for raw in match.group("names").split(","):
            name = re.sub(r"=.*", "", raw).strip()
            name = re.sub(r"\[[^\]]+\]", "", name).strip()
            if re.match(r"^[A-Za-z_][A-Za-z0-9_$]*$", name):
                widths[name] = width
    return widths


def _range_width(range_text: str | None) -> int:
    if not range_text:
        return 1
    match = re.search(r"\[\s*(\d+)\s*:\s*(\d+)\s*\]", range_text)
    if not match:
        return 1
    return abs(int(match.group(1)) - int(match.group(2))) + 1


def _extract_named_connections(conn_text: str) -> dict[str, str]:
    conns: dict[str, str] = {}
    for match in re.finditer(r"\.(?P<port>[A-Za-z_][A-Za-z0-9_$]*)\s*\(\s*(?P<sig>[^()]+?)\s*\)", conn_text):
        sig = match.group("sig").strip()
        sig = re.sub(r"\[[^\]]+\]", "", sig).strip()
        if re.match(r"^[A-Za-z_][A-Za-z0-9_$]*$", sig):
            conns[match.group("port")] = sig
    return conns


def _detect_openram_memory(files: list[str]) -> dict[str, Any] | None:
    inst_re = re.compile(
        r"(?P<cell>[A-Za-z_][A-Za-z0-9_$]*(?:openram|sram)[A-Za-z0-9_$]*)\s*"
        r"(?:#\s*\([^;]*?\)\s*)?(?P<inst>[A-Za-z_][A-Za-z0-9_$]*)\s*\((?P<conns>.*?)\)\s*;",
        flags=re.IGNORECASE | re.DOTALL,
    )
    for path in files:
        text = _read_text(path)
        widths = _parse_declaration_widths(text)
        clean = _strip_comments(text)
        for match in inst_re.finditer(clean):
            cell = match.group("cell")
            inst = match.group("inst")
            conns = _extract_named_connections(match.group("conns"))
            addr_sig = _first_signal(conns, ("addr", "address", "A", "ADDR"))
            data_sig = _first_signal(conns, ("din", "dout", "data", "wdata", "rdata", "D", "Q", "DATA"))
            addr_width = widths.get(addr_sig or "", 8)
            data_width = widths.get(data_sig or "", 32)
            return {
                "cell": cell,
                "instance": inst,
                "source_file": path,
                "connections": conns,
                "addr_width": int(addr_width or 8),
                "data_width": int(data_width or 32),
                "depth": 1 << int(addr_width or 8),
            }
    return None


def _first_signal(conns: dict[str, str], names: tuple[str, ...]) -> str | None:
    lowered = {key.lower(): value for key, value in conns.items()}
    for name in names:
        if name in conns:
            return conns[name]
        if name.lower() in lowered:
            return lowered[name.lower()]
    for key, value in conns.items():
        if any(token.lower() in key.lower() for token in names):
            return value
    return None


def _patch_yaml_value(text: str, key_tokens: tuple[str, ...], value: str) -> tuple[str, bool]:
    lines = text.splitlines()
    changed = False
    pattern = re.compile(r"^(\s*)([A-Za-z0-9_-]+)\s*:\s*(.*)$")
    for i, line in enumerate(lines):
        match = pattern.match(line)
        if not match:
            continue
        key = match.group(2).lower()
        if any(token in key for token in key_tokens):
            lines[i] = f"{match.group(1)}{match.group(2)}: {value}"
            changed = True
    return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), changed


def _patch_autombist_config(config_text: str, memory: dict[str, Any]) -> str:
    text = config_text
    patches = [
        (("memory_name", "mem_name", "name"), str(memory["cell"])),
        (("module", "macro", "sram"), str(memory["cell"])),
        (("data_width", "word_size", "width"), str(memory["data_width"])),
        (("addr_width", "address_width"), str(memory["addr_width"])),
        (("depth", "num_words", "words"), str(memory["depth"])),
    ]
    changed_any = False
    for tokens, value in patches:
        text, changed = _patch_yaml_value(text, tokens, value)
        changed_any = changed_any or changed
    if changed_any:
        return text
    return (
        text.rstrip()
        + "\n\n"
        + "memory_name: {cell}\nmodule_name: {cell}\ndata_width: {data_width}\naddr_width: {addr_width}\ndepth: {depth}\n".format(**memory)
    )


def _copy_tree_files(src_dir: str, dst_dir: str, exts: tuple[str, ...]) -> list[str]:
    copied: list[str] = []
    if not os.path.isdir(src_dir):
        return copied
    for root, _, files in os.walk(src_dir):
        for filename in files:
            if not filename.lower().endswith(exts):
                continue
            src = os.path.join(root, filename)
            rel = os.path.relpath(src, src_dir)
            dst = os.path.join(dst_dir, rel)
            _ensure_dir(os.path.dirname(dst))
            shutil.copy2(src, dst)
            copied.append(os.path.abspath(dst))
    return sorted(copied)


def _module_ports(text: str) -> tuple[str | None, list[str]]:
    match = re.search(
        r"\bmodule\s+(?P<name>[A-Za-z_][A-Za-z0-9_$]*)\s*(?:#\s*\([^;]*?\)\s*)?\((?P<ports>.*?)\)\s*;",
        _strip_comments(text),
        flags=re.DOTALL,
    )
    if not match:
        return None, []
    ports = []
    for raw in match.group("ports").split(","):
        token = raw.strip()
        token = re.sub(r"\b(input|output|inout|wire|logic|reg)\b", " ", token)
        token = re.sub(r"\[[^\]]+\]", " ", token)
        names = re.findall(r"\b[A-Za-z_][A-Za-z0-9_$]*\b", token)
        if names:
            ports.append(names[-1])
    return match.group("name"), ports


def _pick_generated_wrapper(generated_rtl: list[str], memory_cell: str) -> tuple[str | None, list[str]]:
    preferred = os.path.basename(memory_cell).lower() + "_mbist"
    best_name: str | None = None
    best_ports: list[str] = []
    for path in generated_rtl:
        name, ports = _module_ports(_read_text(path))
        if not name:
            continue
        lowered = name.lower()
        if lowered == preferred or lowered.endswith("_mbist") or "mbist" in lowered:
            best_name, best_ports = name, ports
            if lowered == preferred:
                break
    return best_name, best_ports


def _fallback_signal_for_port(port: str, conns: dict[str, str], known_signals: set[str]) -> str:
    p = port.lower()
    for key, sig in conns.items():
        k = key.lower()
        if ("clk" in p or "clock" in p) and ("clk" in k or "clock" in k):
            return sig
        if ("rst" in p or "reset" in p) and ("rst" in k or "reset" in k):
            return sig
    for sig in sorted(known_signals):
        s = sig.lower()
        if ("start" in p or "enable" in p or p.endswith("_en")) and ("bist_start" in s or "mbist_start" in s):
            return sig
        if "done" in p and ("bist_done" in s or "mbist_done" in s):
            return sig
        if "fail" in p and ("bist_fail" in s or "mbist_fail" in s):
            return sig
    if "reset" in p or "rst" in p:
        return "1'b1" if p.endswith("_n") or "reset_n" in p else "1'b0"
    return "1'b0"


def _replace_memory_instance_with_wrapper(memory: dict[str, Any], wrapper_name: str, wrapper_ports: list[str], out_dir: str) -> str | None:
    src = str(memory.get("source_file") or "")
    text = _read_text(src)
    if not text or not wrapper_name or not wrapper_ports:
        return None
    conns = memory.get("connections") if isinstance(memory.get("connections"), dict) else {}
    original_ports = set(conns)
    wrapper_port_set = set(wrapper_ports)
    if not original_ports.intersection(wrapper_port_set):
        return None

    known_signals = set(_parse_declaration_widths(text).keys()) | {str(sig) for sig in conns.values()}
    mapped = []
    for port in wrapper_ports:
        if port in conns:
            sig = conns[port]
        else:
            sig = _fallback_signal_for_port(port, conns, known_signals)
        mapped.append(f".{port}({sig})")
    replacement = f"{wrapper_name} {memory['instance']} (\n    " + ",\n    ".join(mapped) + "\n  );"

    cell = re.escape(str(memory["cell"]))
    inst = re.escape(str(memory["instance"]))
    pattern = re.compile(
        rf"\b{cell}\s*(?:#\s*\([^;]*?\)\s*)?{inst}\s*\(.*?\)\s*;",
        flags=re.DOTALL,
    )
    new_text, count = pattern.subn(replacement, text, count=1)
    if count != 1:
        return None
    dst = os.path.join(out_dir, os.path.basename(src))
    _write_text(dst, new_text)
    return os.path.abspath(dst)


def _simulation_passed(report_dir: str, run_output: str) -> bool:
    latest = os.path.join(report_dir, "latest.json")
    if os.path.exists(latest):
        try:
            data = json.loads(_read_text(latest))
            status = str(data.get("status") or data.get("result") or "").lower()
            if status in {"pass", "passed", "ok", "success"}:
                return True
            if data.get("passed") is True or data.get("success") is True:
                return True
        except Exception:
            pass
    lowered = run_output.lower()
    return "pass" in lowered and "fail" not in lowered and "error" not in lowered


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = os.path.abspath(state.get("workflow_dir") or f"backend/workflows/{workflow_id}")
    stage_dir = os.path.join(workflow_dir, "digital", "mbist_rtl_insertion")
    _ensure_dir(stage_dir)

    toggles = state.get("toggles") if isinstance(state.get("toggles"), dict) else {}
    enabled = bool(
        toggles.get("insert_mbist")
        or toggles.get("enable_mbist_rtl_insertion")
        or state.get("insert_mbist")
        or state.get("enable_mbist_rtl_insertion")
    )
    rtl_files = _rtl_files(state, workflow_dir)
    memory = _detect_openram_memory(rtl_files)
    autombist = tool_path("autombist", state)

    summary: dict[str, Any] = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "enabled": enabled,
        "status": "disabled" if not enabled else "not_started",
        "detected_memory": memory,
        "autombist_executable": autombist,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "rtl_files_scanned": rtl_files,
    }

    if not enabled:
        _write_text(os.path.join(stage_dir, "mbist_rtl_insertion_summary.json"), json.dumps(summary, indent=2))
        state.setdefault("digital", {})["mbist_rtl_insertion"] = summary
        state["status"] = f"{AGENT_NAME}: disabled"
        return state
    if not memory:
        summary["status"] = "skipped_no_openram_sram_detected"
        _write_text(os.path.join(stage_dir, "mbist_rtl_insertion_summary.json"), json.dumps(summary, indent=2))
        state.setdefault("digital", {})["mbist_rtl_insertion"] = summary
        state["status"] = f"{AGENT_NAME}: no OpenRAM/SRAM memory detected"
        return state
    if not autombist:
        summary["status"] = "failed"
        summary["reason"] = "autombist_not_found"
        _write_text(os.path.join(stage_dir, "mbist_rtl_insertion_summary.json"), json.dumps(summary, indent=2))
        raise RuntimeError("MBIST RTL insertion requested but autombist was not found in tool profile/PATH.")

    project_dir = os.path.join(stage_dir, "autombist_project")
    _ensure_dir(project_dir)
    rc_init, out_init = _run([autombist, "init", "--out", project_dir, "--force"], cwd=stage_dir, timeout=120)
    _write_text(os.path.join(stage_dir, "autombist_init.log"), out_init)
    config_path = os.path.join(project_dir, "config.yml")
    if rc_init != 0 or not os.path.exists(config_path):
        summary["status"] = "failed"
        summary["reason"] = "autombist_init_failed"
        summary["init_rc"] = rc_init
        _write_text(os.path.join(stage_dir, "mbist_rtl_insertion_summary.json"), json.dumps(summary, indent=2))
        raise RuntimeError("MBIST RTL insertion failed: autombist init did not produce config.yml.")

    patched_config = _patch_autombist_config(_read_text(config_path), memory)
    _write_text(config_path, patched_config)
    _write_text(os.path.join(stage_dir, "config.yml"), patched_config)

    out_dir = os.path.join(stage_dir, "autombist_out")
    rc_run, out_run = _run(
        [autombist, "run", "--config", config_path, "--out", out_dir, "--test"],
        cwd=stage_dir,
        timeout=900,
    )
    _write_text(os.path.join(stage_dir, "autombist_run.log"), out_run)

    generated_root = os.path.join(out_dir, str(memory["cell"]))
    if not os.path.isdir(generated_root):
        hits = [path for path in glob.glob(os.path.join(out_dir, "*")) if os.path.isdir(path)]
        generated_root = hits[0] if hits else generated_root
    reports_dir = os.path.join(generated_root, "reports")
    sim_passed = rc_run == 0 and _simulation_passed(reports_dir, out_run)
    if not sim_passed:
        summary.update({
            "status": "failed",
            "reason": "autombist_standalone_simulation_failed",
            "run_rc": rc_run,
            "run_log": os.path.join(stage_dir, "autombist_run.log"),
        })
        _write_text(os.path.join(stage_dir, "mbist_rtl_insertion_summary.json"), json.dumps(summary, indent=2))
        raise RuntimeError("MBIST RTL insertion failed: AutoMBIST standalone simulation did not pass.")

    final_rtl_dir = os.path.join(stage_dir, "integrated_rtl")
    generated_rtl = _copy_tree_files(generated_root, final_rtl_dir, (".v", ".sv", ".vh", ".svh"))
    original_rtl_dir = os.path.join(final_rtl_dir, "functional_rtl")
    _ensure_dir(original_rtl_dir)
    wrapper_name, wrapper_ports = _pick_generated_wrapper(generated_rtl, str(memory["cell"]))
    patched_source = _replace_memory_instance_with_wrapper(memory, wrapper_name or "", wrapper_ports, original_rtl_dir)
    if not patched_source:
        summary.update({
            "status": "failed",
            "reason": "mbist_wrapper_parent_integration_failed",
            "wrapper_module": wrapper_name,
            "wrapper_ports": wrapper_ports,
        })
        _write_text(os.path.join(stage_dir, "mbist_rtl_insertion_summary.json"), json.dumps(summary, indent=2))
        raise RuntimeError("MBIST RTL insertion failed: generated AutoMBIST wrapper could not be safely integrated into the functional RTL top.")
    for src in rtl_files:
        if patched_source and os.path.abspath(src) == os.path.abspath(str(memory.get("source_file"))):
            continue
        shutil.copy2(src, os.path.join(original_rtl_dir, os.path.basename(src)))

    final_files = sorted(dict.fromkeys([*generated_rtl, *glob.glob(os.path.join(original_rtl_dir, "*.v")), *glob.glob(os.path.join(original_rtl_dir, "*.sv"))]))
    integration_status = "wrapper_replaced_memory_instance"
    summary.update({
        "status": "mbist_rtl_generated_and_simulated",
        "simulation": {"status": "pass", "reports_dir": reports_dir, "run_log": os.path.join(stage_dir, "autombist_run.log")},
        "integration_status": integration_status,
        "wrapper_module": wrapper_name,
        "patched_source": patched_source,
        "integrated_rtl_dir": final_rtl_dir,
        "final_rtl_files": final_files,
        "integration_note": (
            "AutoMBIST generated and simulated wrapper RTL. The agent replaces the detected OpenRAM/SRAM instance when wrapper ports can be mapped; "
            "otherwise it fails later at synthesis rather than claiming a fake insertion."
        ),
    })

    _write_text(os.path.join(stage_dir, "mbist_rtl_insertion_summary.json"), json.dumps(summary, indent=2))
    report = "\n".join([
        "# MBIST RTL Insertion",
        "",
        f"- Status: `{summary['status']}`",
        f"- Memory cell: `{memory['cell']}`",
        f"- Memory instance: `{memory['instance']}`",
        f"- Data width: `{memory['data_width']}`",
        f"- Address width: `{memory['addr_width']}`",
        f"- AutoMBIST simulation: `pass`",
        f"- Integration status: `{integration_status}`",
        "",
    ])
    _write_text(os.path.join(stage_dir, "mbist_rtl_insertion.md"), report)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital/mbist_rtl_insertion", "mbist_rtl_insertion_summary.json", json.dumps(summary, indent=2))
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital/mbist_rtl_insertion", "mbist_rtl_insertion.md", report)

    digital = state.setdefault("digital", {})
    digital["mbist_rtl_insertion"] = summary
    digital["rtl_files"] = final_files
    state["rtl_files"] = final_files
    state["mbist_rtl_inserted"] = True
    state["status"] = f"{AGENT_NAME}: {summary['status']}"
    return state

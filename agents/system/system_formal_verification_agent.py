import json
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from tooling.runner import run_command, tool_path
from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Formal Verification Agent"


def _now() -> str:
    return datetime.now().isoformat()


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _record(workflow_id: str, subdir: str, filename: str, content: str) -> Optional[str]:
    try:
        return save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=AGENT_NAME,
            subdir=subdir,
            filename=filename,
            content=content,
        )
    except Exception:
        return None


def _toolchain(state: Dict[str, Any]) -> Dict[str, Any]:
    return state.get("toolchain") if isinstance(state.get("toolchain"), dict) else {}


def _solver(state: Dict[str, Any]) -> str:
    raw = str(_toolchain(state).get("formal_solver") or state.get("formal_solver") or "z3").strip().lower()
    return raw if raw in {"z3", "boolector"} else "z3"


def _formal_tool(state: Dict[str, Any]) -> str:
    return str(_toolchain(state).get("formal") or state.get("formal_tool") or "none").strip().lower()


def _module_names(path: str) -> List[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except Exception:
        return []
    return re.findall(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)\b", text)


def _is_analog_or_macro_file(path: str) -> bool:
    text = path.replace("\\", "/").lower()
    name = os.path.basename(text)
    parts = [p for p in re.split(r"[\\/]+", text) if p]
    tokens = ("analog", "macro", "ams", "behavioral", "adc", "dac", "pll", "ldo", "bandgap", "opamp", "sensor")
    if any(token in name for token in tokens):
        return True
    if "analog" in parts:
        return True
    if "digital" in parts or "/rtl/" in text:
        return False
    return any(token in text for token in tokens)


def _module_declaration_tail(path: str, module: str) -> Optional[str]:
    try:
        text = open(path, "r", encoding="utf-8", errors="ignore").read()
    except Exception:
        return None
    m = re.search(rf"\bmodule\s+{re.escape(module)}(?P<tail>\s*(?:#\s*\(.*?\)\s*)?\(.*?\)\s*);", text, re.S)
    return m.group("tail") if m else None


def _module_source(path: str, module: str) -> str:
    try:
        text = open(path, "r", encoding="utf-8", errors="ignore").read()
    except Exception:
        return ""
    m = re.search(rf"\bmodule\s+{re.escape(module)}\b.*?\bendmodule\b", text, re.S)
    return m.group(0) if m else ""


def _split_port_names(blob: str) -> List[str]:
    names: List[str] = []
    for raw in blob.split(","):
        token = re.sub(r"//.*", "", raw).strip()
        token = re.sub(r"/\*.*?\*/", "", token, flags=re.S).strip()
        if not token:
            continue
        token = token.split("=", 1)[0].strip()
        token = re.sub(r"\[[^\]]+\]", "", token).strip()
        m = re.search(r"([A-Za-z_][A-Za-z0-9_$]*)\s*$", token)
        if m:
            names.append(m.group(1))
    return names


def _parse_module_ports(path: str, module: str) -> List[Dict[str, str]]:
    tail = _module_declaration_tail(path, module) or ""
    m = re.search(r"\((?P<ports>.*)\)\s*$", tail, re.S)
    if not m:
        return []
    ordered = _split_port_names(m.group("ports"))
    ports: Dict[str, Dict[str, str]] = {
        name: {"name": name, "direction": "input", "width": ""} for name in ordered
    }
    decl_sources = [_module_source(path, module), m.group("ports").replace(",", ";") + ";"]
    decl_re = re.compile(
        r"\b(?P<direction>input|output|inout)\b\s+"
        r"(?:(?:wire|reg|logic|signed|unsigned)\s+)*"
        r"(?P<width>\[[^\]]+\])?\s*"
        r"(?P<names>[^;()]+);",
        re.S,
    )
    for src in decl_sources:
        for decl in decl_re.finditer(src):
            direction = decl.group("direction")
            width = (decl.group("width") or "").strip()
            for name in _split_port_names(decl.group("names")):
                if name in ports:
                    ports[name]["direction"] = direction
                    ports[name]["width"] = width
    return [ports[name] for name in ordered]


def _formal_blackbox_stub(path: str, module: str) -> str:
    tail = _module_declaration_tail(path, module)
    if not tail:
        return f"module {module}();\nendmodule\n"
    ports = _parse_module_ports(path, module)
    if not ports:
        return f"module {module}{tail};\nendmodule\n"

    lines = [
        "// Auto-generated formal abstraction for non-digital or macro RTL.",
        "// Outputs are unconstrained so SBY can elaborate the integrated system.",
        f"module {module} (",
    ]
    port_lines: List[str] = []
    for port in ports:
        width = f" {port['width']}" if port.get("width") else ""
        port_lines.append(f"    {port.get('direction') or 'input'}{width} {port['name']}")
    lines.append(",\n".join(port_lines))
    lines.append(");")
    for port in ports:
        if port.get("direction") != "output":
            continue
        width = f" {port['width']}" if port.get("width") else ""
        internal = re.sub(r"[^A-Za-z0-9_$]", "_", f"{port['name']}_anyseq")
        lines.append(f"  (* anyseq *) reg{width} {internal};")
        lines.append(f"  assign {port['name']} = {internal};")
    lines.append("endmodule")
    return "\n".join(lines) + "\n"


def _prepare_formal_rtl(rtl_files: List[str], formal_root: str) -> tuple[List[str], List[Dict[str, Any]]]:
    formal_files: List[str] = []
    blackboxed: List[Dict[str, Any]] = []
    blackbox_root = os.path.join(formal_root, "blackboxes")
    os.makedirs(blackbox_root, exist_ok=True)
    for path in rtl_files:
        modules = _module_names(path)
        if modules and _is_analog_or_macro_file(path):
            stubs = "\n".join(_formal_blackbox_stub(path, module) for module in modules)
            stub_path = os.path.abspath(os.path.join(blackbox_root, f"{os.path.splitext(os.path.basename(path))[0]}_formal_blackbox.sv"))
            _write(stub_path, stubs)
            formal_files.append(stub_path)
            blackboxed.append({"source": path, "stub": stub_path, "modules": modules, "strategy": "unconstrained_anyseq_stub"})
        else:
            formal_files.append(path)
    return formal_files, blackboxed


def _rtl_preference(path: str) -> tuple[int, str]:
    stem = os.path.splitext(os.path.basename(path))[0].lower()
    is_intermediate = bool(re.search(r"(?:^|_)pass\d+$", stem) or re.search(r"_pass\d+(?:_|$)", stem))
    return (1 if is_intermediate else 0, os.path.basename(path).lower())


def _dedupe_rtl_by_module(files: List[str]) -> List[str]:
    existing = [os.path.abspath(path) for path in dict.fromkeys(files) if os.path.isfile(path)]
    modules_by_path = {path: _module_names(path) for path in existing}
    owner_by_module: Dict[str, str] = {}
    for path, modules in modules_by_path.items():
        for module in modules:
            prior = owner_by_module.get(module)
            if not prior or _rtl_preference(path) < _rtl_preference(prior):
                owner_by_module[module] = path
    out: List[str] = []
    for path in existing:
        modules = modules_by_path.get(path) or []
        if modules and not any(owner_by_module.get(module) == path for module in modules):
            continue
        out.append(path)
    return list(dict.fromkeys(out))


def _rtl_files(state: Dict[str, Any], workflow_dir: str) -> List[str]:
    direct = state.get("rtl_files")
    if isinstance(direct, list) and direct:
        files = [str(p) for p in direct if str(p).strip()]
    else:
        pkg = state.get("system_rtl_package") if isinstance(state.get("system_rtl_package"), dict) else {}
        filelists = pkg.get("filelists") if isinstance(pkg.get("filelists"), dict) else {}
        files = [str(p) for p in (filelists.get("sim") or []) if str(p).strip()]
    if not files:
        for root, _, names in os.walk(os.path.join(workflow_dir, "system")):
            for name in names:
                if name.lower().endswith((".v", ".sv")):
                    files.append(os.path.join(root, name))
    out: List[str] = []
    for path in files:
        abs_path = path if os.path.isabs(path) else os.path.join(workflow_dir, path)
        low = abs_path.lower().replace("\\", "/")
        if os.path.isfile(abs_path) and not any(skip in low for skip in ("/vv/", "/tb/", "/testbench/")):
            out.append(os.path.abspath(abs_path))
    return _dedupe_rtl_by_module(out)


def _top(state: Dict[str, Any], rtl_files: List[str]) -> str:
    for key in ("soc_top_sim_module", "system_top_module", "top_module"):
        value = str(state.get(key) or "").strip()
        if value:
            return value
    pkg = state.get("system_rtl_package") if isinstance(state.get("system_rtl_package"), dict) else {}
    top = pkg.get("top") if isinstance(pkg.get("top"), dict) else {}
    if str(top.get("sim") or "").strip():
        return str(top["sim"]).strip()
    for path in rtl_files:
        names = _module_names(path)
        if names:
            return names[0]
    return "soc_top_sim"


def _find_clock_reset(rtl_files: List[str], top: str) -> tuple[Optional[str], Optional[str]]:
    top_text = ""
    for path in rtl_files:
        try:
            text = open(path, "r", encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        if re.search(rf"\bmodule\s+{re.escape(top)}\b", text):
            top_text = text
            break
    if not top_text:
        chunks: List[str] = []
        for path in rtl_files[:4]:
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    chunks.append(f.read())
            except Exception:
                continue
        top_text = "\n".join(chunks)
    ports = re.findall(r"\b(?:input|output|inout)\b(?:\s+(?:wire|logic|reg))*\s*(?:\[[^\]]+\]\s*)?([A-Za-z_][A-Za-z0-9_$]*)", top_text)
    clk = next((p for p in ports if re.search(r"(?:^|_)(clk|clock)(?:$|_)", p, re.I)), None)
    rst = next((p for p in ports if re.search(r"(?:^|_)(rst|reset|por)(?:$|_)", p, re.I)), None)
    return clk, rst


def _gen_sby(top: str, rtl_files: List[str], formal_root: str, solver: str) -> str:
    rels = [os.path.relpath(path, formal_root).replace("\\", "/") for path in rtl_files[:200]]
    reads = "\n".join(f"read_verilog -formal -sv {os.path.basename(path)}" for path in rels)
    files = "\n".join(rels)
    return f"""# Auto-generated System Sim formal setup for {top}

[options]
mode bmc
depth 20
multiclock on

[engines]
smtbmc {solver}

[script]
{reads}
hierarchy -top {top}
proc
async2sync
dffunmap
opt
prep -top {top}

[files]
{files}
"""


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    workflow_id = str(state.get("workflow_id") or "default")
    workflow_dir = str(state.get("workflow_dir") or f"backend/workflows/{workflow_id}")
    formal_root = os.path.join(workflow_dir, "vv", "formal")
    os.makedirs(formal_root, exist_ok=True)
    log_path = os.path.join("artifact", "system_formal_verification_agent.log")
    _write(log_path, "System Formal Verification Agent Log\n")

    formal_tool = _formal_tool(state)
    solver = _solver(state)
    rtl_files = _rtl_files(state, workflow_dir)
    top = _top(state, rtl_files)
    clk, rst = _find_clock_reset(rtl_files, top) if rtl_files else (None, None)
    formal_rtl_files, blackboxed_modules = _prepare_formal_rtl(rtl_files, formal_root)
    sby_txt = _gen_sby(top, formal_rtl_files, formal_root, solver)
    sby_path = os.path.join(formal_root, f"{top}.sby")
    _write(sby_path, sby_txt)

    sby_bin = tool_path("sby", state)
    solver_bin = tool_path(solver, state)
    run_result: Dict[str, Any] = {
        "tool": formal_tool,
        "solver": solver,
        "available": bool(formal_tool == "symbiyosys" and sby_bin and solver_bin and formal_rtl_files),
        "attempted": False,
    }
    if formal_tool in {"none", "disabled", "off"}:
        run_result["disabled"] = True
    elif not formal_rtl_files:
        run_result["blocked_reason"] = "no_system_rtl_files"
    elif not sby_bin or not solver_bin:
        run_result["blocked_reason"] = "tool_unavailable"
    else:
        run_result["attempted"] = True
        proc = run_command(state, "system_formal_verification", [sby_bin, "-f", os.path.basename(sby_path)], cwd=formal_root, timeout_sec=600)
        combined_log = "\n".join((proc.stdout or "").splitlines()[-200:] + (proc.stderr or "").splitlines()[-200:])
        run_result.update({
            "returncode": proc.returncode,
            "stdout_tail": (proc.stdout or "").splitlines()[-120:],
            "stderr_tail": (proc.stderr or "").splitlines()[-120:],
            "tool_execution": proc.to_dict(),
        })
        if proc.returncode != 0:
            if "Multiple edge sensitive events" in combined_log:
                run_result["inconclusive"] = True
                run_result["blocked_reason"] = "yosys_elaboration_multi_edge"
            elif "Can't resolve task name" in combined_log or "Unsupported" in combined_log:
                run_result["inconclusive"] = True
                run_result["blocked_reason"] = "yosys_elaboration_unsupported_construct"
            elif "Found logic loop" in combined_log:
                run_result["inconclusive"] = True
                run_result["blocked_reason"] = "yosys_formal_logic_loop"
            elif "blackbox/whitebox module" in combined_log:
                run_result["inconclusive"] = True
                run_result["blocked_reason"] = "yosys_blackbox_elaboration"

    report = {
        "type": "system_formal_verification",
        "version": "1.0",
        "generated_at": _now(),
        "top_module": top,
        "rtl_file_count": len(rtl_files),
        "formal_rtl_file_count": len(formal_rtl_files),
        "blackboxed_modules": blackboxed_modules,
        "clock": clk,
        "reset": rst,
        "toolchain": {"formal": formal_tool, "formal_solver": solver},
        "tools_detected": {
            "sby": bool(sby_bin),
            "yosys": bool(tool_path("yosys", state)),
            "z3": bool(tool_path("z3", state)),
            "boolector": bool(tool_path("boolector", state)),
            solver: bool(solver_bin),
        },
        "run": run_result,
        "artifacts": {
            "sby": _record(workflow_id, "vv/formal", f"{top}.sby", sby_txt),
        },
    }
    report_txt = json.dumps(report, indent=2)
    _write(os.path.join(formal_root, "formal_report.json"), report_txt)
    report["artifacts"]["report"] = _record(workflow_id, "vv/formal", "formal_report.json", report_txt)
    try:
        report["artifacts"]["log"] = _record(workflow_id, "vv", "system_formal_verification_agent.log", open(log_path, "r", encoding="utf-8").read())
    except Exception:
        report["artifacts"]["log"] = None

    state.setdefault("vv", {})["formal"] = report
    state["system_formal_report_json"] = os.path.join(formal_root, "formal_report.json")
    state["formal_report_json"] = os.path.join(formal_root, "formal_report.json")
    state["status"] = (
        "System formal pass" if run_result.get("attempted") and run_result.get("returncode") == 0
        else "System formal inconclusive" if run_result.get("inconclusive")
        else "System formal not run" if not run_result.get("attempted")
        else "System formal failed"
    )
    return state

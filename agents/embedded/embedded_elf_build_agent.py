
import json
import logging
import os
import shutil
import subprocess
from typing import Optional

from ._embedded_common import ensure_workflow_dir, write_artifact

logger = logging.getLogger(__name__)

AGENT_NAME = "Embedded ELF Build Agent"
PHASE = "elf_build"

OUTPUT_PATH = "firmware/build/build_instructions.md"
OUTPUT_CARGO_TOML = "firmware/build/Cargo.toml"
OUTPUT_CARGO_CFG = "firmware/build/.cargo/config.toml"
OUTPUT_MEMORY_X = "firmware/build/memory.x"
OUTPUT_MAIN_RS = "firmware/src/main.rs"
OUTPUT_PANIC_RS = "firmware/src/panic.rs"
DEBUG_PATH = "firmware/debug/elf_build_result.json"
TOOLCHAIN_DEBUG_PATH = "firmware/debug/elf_toolchain_debug.json"
MANIFEST_PATH = "firmware/firmware_manifest.json"


def _safe_load_json(path: str) -> Optional[dict]:
    try:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as exc:
        logger.warning("%s failed loading %s: %s", AGENT_NAME, path, exc)
    return None


def _load_manifest(state: dict, workflow_dir: str) -> dict:
    manifest = state.get("firmware_manifest") or (state.get("firmware") or {}).get("manifest")
    if isinstance(manifest, dict):
        return dict(manifest)
    manifest_path = state.get("firmware_manifest_path") or (state.get("firmware") or {}).get("manifest_path") or MANIFEST_PATH
    if manifest_path and not os.path.isabs(manifest_path):
        manifest_path = os.path.join(workflow_dir, manifest_path)
    loaded = _safe_load_json(manifest_path)
    return loaded if isinstance(loaded, dict) else {}


def _write_json_artifact(state: dict, relpath: str, payload: dict) -> None:
    write_artifact(state, relpath, json.dumps(payload, indent=2), key=os.path.basename(relpath))


def _resolve_toolchain(state: dict, manifest: dict) -> tuple[str, str]:
    toolchain = state.get("toolchain") or {}
    target_triple = (
        toolchain.get("target_triple")
        or state.get("target_triple")
        or (manifest.get("build") or {}).get("target_triple")
        or "x86_64-unknown-linux-gnu"
    ).strip()
    bin_name = (
        toolchain.get("bin_name")
        or state.get("firmware_bin_name")
        or "firmware_app"
    ).strip()
    return target_triple, bin_name


def _default_cargo_toml(bin_name: str) -> str:
    return f"""[package]
name = "firmware_workspace"
version = "0.1.0"
edition = "2021"

[[bin]]
name = "{bin_name}"
path = "../src/main.rs"

[profile.release]
panic = "abort"
lto = false
codegen-units = 1
"""


def _default_cargo_config(target_triple: str) -> str:
    return f"""[build]
target = "{target_triple}"
"""


def _default_memory_x() -> str:
    return """MEMORY
{
  FLASH (rx)  : ORIGIN = 0x00000000, LENGTH = 256K
  RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 64K
}
"""


def _default_panic_rs() -> str:
    return """use core::panic::PanicInfo;

#[panic_handler]
fn panic(_info: &PanicInfo) -> ! {
    loop {}
}
"""


def _default_main_rs(hal_read_helper: Optional[str] = None) -> str:
    hal_probe = ""
    if hal_read_helper:
        hal_probe = f"""
#[path = "../hal/registers.rs"]
mod registers;
"""
    body_probe = ""
    if hal_read_helper:
        body_probe = f"""
    let _ = registers::{hal_read_helper}();
"""

    return f"""#![no_std]
#![no_main]

mod panic;{hal_probe}

#[no_mangle]
pub extern "C" fn _start() -> ! {{{body_probe}
    loop {{}}
}}
"""

def _discover_hal_read_helper(state: dict, workflow_dir: str) -> Optional[str]:
    hal_code = state.get("firmware_hal_code") or (state.get("firmware") or {}).get("hal_code")
    if not hal_code:
        hal_path = state.get("firmware_hal_path") or "firmware/hal/registers.rs"
        if hal_path and not os.path.isabs(hal_path):
            hal_path = os.path.join(workflow_dir, hal_path)
        try:
            if hal_path and os.path.isfile(hal_path):
                with open(hal_path, "r", encoding="utf-8") as f:
                    hal_code = f.read()
        except Exception as exc:
            logger.warning("%s failed reading HAL file %s: %s", AGENT_NAME, hal_path, exc)
            hal_code = ""

    if not hal_code:
        return None

    import re
    matches = re.findall(r"pub\s+fn\s+(read_[A-Za-z0-9_]+)\s*\(", hal_code)
    return matches[0] if matches else None


def _default_build_instructions(target_triple: str, bin_name: str) -> str:
    return f"""<!-- ASSUMPTION: Build executed inside the ChipLoop runtime image -->
<!-- ASSUMPTION: Cargo and the requested target toolchain are already installed -->

# Build Instructions

## Build ELF

cargo build --release --target {target_triple}

## Expected Cargo Output

target/{target_triple}/release/{bin_name}

## Optional Canonical ELF Copy

mkdir -p firmware/build/target/{target_triple}/release
cp target/{target_triple}/release/{bin_name} firmware/build/target/{target_triple}/release/{bin_name}.elf

## Validate ELF Exists

ls firmware/build/target/{target_triple}/release/{bin_name}.elf
"""



def _write_workspace_files(state: dict, workflow_dir: str, target_triple: str, bin_name: str) -> None:
    hal_read_helper = _discover_hal_read_helper(state, workflow_dir)

    files = {
        OUTPUT_CARGO_TOML: _default_cargo_toml(bin_name),
        OUTPUT_CARGO_CFG: _default_cargo_config(target_triple),
        OUTPUT_MEMORY_X: _default_memory_x(),
        OUTPUT_MAIN_RS: _default_main_rs(hal_read_helper),
        OUTPUT_PANIC_RS: _default_panic_rs(),
        OUTPUT_PATH: _default_build_instructions(target_triple, bin_name),
    }

    for relpath, content in files.items():
        write_artifact(state, relpath, content, key=os.path.basename(relpath))


def _attempt_build(workflow_dir: str, target_triple: str, bin_name: str) -> tuple[bool, bool, str, str, str]:
    cargo_workspace_dir = os.path.join(workflow_dir, "firmware", "build")
    cargo_target_abs = os.path.join(cargo_workspace_dir, "target", target_triple, "release", bin_name)
    build_attempted = False
    build_succeeded = False
    stdout = ""
    stderr = ""

    cargo_path = shutil.which("cargo")
    if cargo_path and os.path.isdir(cargo_workspace_dir):
        try:
            build_attempted = True
            proc = subprocess.run(
                [cargo_path, "build", "--release", "--target", target_triple],
                cwd=cargo_workspace_dir,
                capture_output=True,
                text=True,
                timeout=180,
            )
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            build_succeeded = proc.returncode == 0 and os.path.isfile(cargo_target_abs)
        except Exception as exc:
            stderr = str(exc)
    return build_attempted, build_succeeded, stdout, stderr, cargo_target_abs


def run_agent(state: dict) -> dict:
    print(f"\n🚀 Running {AGENT_NAME}...")
    logger.info("Starting %s", AGENT_NAME)
    ensure_workflow_dir(state)

    workflow_dir = state.get("workflow_dir") or ""
    manifest = _load_manifest(state, workflow_dir)
    target_triple, bin_name = _resolve_toolchain(state, manifest)

    _write_workspace_files(state, workflow_dir, target_triple, bin_name)

    cargo_workspace_dir = os.path.join(workflow_dir, "firmware", "build")
    required_srcs = [
        os.path.join(workflow_dir, OUTPUT_MAIN_RS),
        os.path.join(workflow_dir, OUTPUT_PANIC_RS),
        os.path.join(workflow_dir, OUTPUT_CARGO_TOML),
        os.path.join(workflow_dir, OUTPUT_CARGO_CFG),
    ]
    optional_srcs = [
        os.path.join(workflow_dir, "firmware/hal/registers.rs"),
        os.path.join(workflow_dir, "firmware/drivers/driver_scaffold.rs"),
        os.path.join(workflow_dir, "firmware/diagnostics/register_dump.rs"),
    ]
    workspace_generated = all(os.path.isfile(p) for p in required_srcs) if workflow_dir else False

    _write_json_artifact(
        state,
        TOOLCHAIN_DEBUG_PATH,
        {
            "agent": AGENT_NAME,
            "workflow_dir": workflow_dir,
            "cargo_workspace_dir": cargo_workspace_dir,
            "cargo_workspace_dir_exists": os.path.isdir(cargo_workspace_dir),
            "target_triple": target_triple,
            "bin_name": bin_name,
            "toolchain": state.get("toolchain"),
            "required_srcs_abs": required_srcs,
            "required_srcs_exists": {p: os.path.isfile(p) for p in required_srcs},
            "optional_srcs_abs": optional_srcs,
            "optional_srcs_exists": {p: os.path.isfile(p) for p in optional_srcs},
            "workspace_generated": workspace_generated,
        },
    )

    elf_relpath = f"firmware/build/target/{target_triple}/release/{bin_name}.elf"
    cargo_target_abs = os.path.join(cargo_workspace_dir, "target", target_triple, "release", bin_name)
    build_attempted = False
    build_succeeded = False
    build_stdout = ""
    build_stderr = ""
    elf_exists = False

    missing_required = [
        os.path.relpath(p, workflow_dir).replace("\\", "/")
        for p in required_srcs
        if workflow_dir and not os.path.isfile(p)
    ]

    
    if missing_required:
        _write_json_artifact(
            state,
            DEBUG_PATH,
            {
                "agent": AGENT_NAME,
                "workflow_dir": workflow_dir,
                "cwd_used_for_build": cargo_workspace_dir,
                "target_triple": target_triple,
                "bin_name": bin_name,
                "cargo_workspace_dir": cargo_workspace_dir,
                "cargo_workspace_dir_exists": os.path.isdir(cargo_workspace_dir),
                "cargo_target_abs": cargo_target_abs,
                "canonical_elf_relpath": elf_relpath,
                "required_srcs_abs": required_srcs,
                "required_srcs_exists": {p: os.path.isfile(p) for p in required_srcs},
                "optional_srcs_abs": optional_srcs,
                "optional_srcs_exists": {p: os.path.isfile(p) for p in optional_srcs},
                "workspace_generated": workspace_generated,
                "missing_required_files": missing_required,
                "build_attempted": False,
                "build_succeeded": False,
                "elf_exists": False,
                "stdout_tail": "",
                "stderr_tail": "Required firmware build files missing before cargo build.",
            },
        )

        state["status"] = "⚠️ ELF build blocked: required firmware build files missing"
        return state

        
    build_attempted, build_succeeded, build_stdout, build_stderr, cargo_target_abs = _attempt_build(workflow_dir, target_triple, bin_name)

    elf_relpath = f"firmware/build/target/{target_triple}/release/{bin_name}.elf"
    elf_abs = os.path.join(workflow_dir, elf_relpath)

    if build_succeeded:
        try:
            os.makedirs(os.path.dirname(elf_abs), exist_ok=True)
            shutil.copy2(cargo_target_abs, elf_abs)
        except Exception as exc:
            build_stderr = ((build_stderr + "\n") if build_stderr else "") + str(exc)
            build_succeeded = False

    elf_exists = os.path.isfile(elf_abs)

    state["firmware_elf_path"] = elf_relpath
    state["firmware_expected_elf_path"] = elf_relpath
    state["elf_path"] = elf_relpath
    state["embedded_elf_path"] = elf_relpath
    state["firmware_elf_exists"] = elf_exists

    build_block = state.setdefault("firmware_build", {})
    build_block["target_triple"] = target_triple
    build_block["bin_name"] = bin_name
    build_block["elf_path"] = elf_relpath
    build_block["elf_exists"] = elf_exists
    build_block["build_attempted"] = build_attempted
    build_block["build_succeeded"] = build_succeeded
    build_block["cargo_target_abs"] = cargo_target_abs
    build_block["build_stdout"] = build_stdout[-4000:]
    build_block["build_stderr"] = build_stderr[-4000:]
    build_block["build_instructions_path"] = OUTPUT_PATH

    manifest = dict(manifest or {})
    build_manifest = dict(manifest.get("build") or {})
    build_manifest["target_triple"] = target_triple
    build_manifest["build_root"] = "firmware/build"
    manifest["build"] = build_manifest
    manifest["elf_path"] = elf_relpath
    write_artifact(state, MANIFEST_PATH, json.dumps(manifest, indent=2), key=os.path.basename(MANIFEST_PATH))
    state["firmware_manifest"] = manifest
    state["firmware_manifest_path"] = MANIFEST_PATH

        _write_json_artifact(
        state,
        DEBUG_PATH,
        {
            "agent": AGENT_NAME,
            "workflow_dir": workflow_dir,
            "cwd_used_for_build": cargo_workspace_dir,
            "target_triple": target_triple,
            "bin_name": bin_name,
            "cargo_workspace_dir": cargo_workspace_dir,
            "cargo_workspace_dir_exists": os.path.isdir(cargo_workspace_dir),
            "cargo_target_abs": cargo_target_abs,
            "canonical_elf_relpath": elf_relpath,
            "required_srcs_abs": required_srcs,
            "required_srcs_exists": {p: os.path.isfile(p) for p in required_srcs},
            "optional_srcs_abs": optional_srcs,
            "optional_srcs_exists": {p: os.path.isfile(p) for p in optional_srcs},
            "workspace_generated": workspace_generated,
            "build_attempted": build_attempted,
            "build_succeeded": build_succeeded,
            "elf_exists": elf_exists,
            "stdout_tail": build_stdout[-4000:],
            "stderr_tail": build_stderr[-4000:],
        },
    )

    

    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH
    state["status"] = f"✅ {AGENT_NAME} done" if elf_exists or build_attempted else f"✅ {AGENT_NAME} generated build workspace"
    return state

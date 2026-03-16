import json
from ._embedded_common import ensure_workflow_dir, llm_chat, write_artifact, strip_markdown_fences_for_code
import os


AGENT_NAME = "Embedded ELF Build Agent"
PHASE = "elf_build"

# IMPORTANT: no trailing spaces
OUTPUT_PATH = "firmware/build/build_instructions.md"

# Workspace outputs (merged “firmware workspace agent” into this agent)
OUTPUT_CARGO_TOML = "firmware/build/Cargo.toml"
OUTPUT_CARGO_CFG  = "firmware/build/.cargo/config.toml"
OUTPUT_MEMORY_X   = "firmware/build/memory.x"
OUTPUT_LIB_RS     = "firmware/src/main.rs"
OUTPUT_PANIC_RS   = "firmware/src/panic.rs"


def _write_build_result(state, payload: dict):
    write_artifact(
        state,
        "firmware/debug/elf_build_result.json",
        json.dumps(payload, indent=2),
        key="elf_build_result.json",
    )

def _safe_read(path):
    try:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
    except Exception:
        pass
    return ""

def run_agent(state: dict) -> dict:
    print(f"\n🚀 Running {AGENT_NAME}...")
    ensure_workflow_dir(state)

    workflow_dir = state.get("workflow_dir") or ""



    regmap_obj = (
        state.get("firmware_register_map")
        or (state.get("firmware") or {}).get("register_map")
    )

    hal_text = (
        state.get("firmware_hal_code")
        or (state.get("firmware") or {}).get("hal_code")
        or _safe_read(os.path.join(workflow_dir, "firmware/hal/registers.rs"))
    )

    driver_text = (
        state.get("firmware_driver_code")
        or (state.get("firmware") or {}).get("driver_code")
        or _safe_read(os.path.join(workflow_dir, "firmware/drivers/driver_scaffold.rs"))
    )



    regmap_text = json.dumps(regmap_obj, indent=2) if regmap_obj else _safe_read(os.path.join(workflow_dir, "firmware/register_map.json"))

    spec_text = (state.get("spec_text") or state.get("spec") or "").strip()
    goal = (state.get("goal") or "").strip()
    toolchain = state.get("toolchain") or {}
    toggles = state.get("toggles") or {}

    write_artifact(
        state,
        "firmware/debug/elf_toolchain_debug.json",
        json.dumps({
            "agent": AGENT_NAME,
            "state_target_triple": state.get("target_triple"),
            "state_firmware_bin_name": state.get("firmware_bin_name"),
            "toolchain": state.get("toolchain"),
        }, indent=2),
        key="elf_toolchain_debug.json",
    )

    resolved_target_triple = (
        toolchain.get("target_triple")
        or state.get("target_triple")
        or ""
    ).strip()

    bin_name = (
        toolchain.get("bin_name")
        or state.get("firmware_bin_name")
        or "firmware_app"
    ).strip()


    if not resolved_target_triple:
        state["status"] = "❌ target_triple missing in state for ELF build generation"
        _write_build_result(state, {
            "agent": AGENT_NAME,
            "target_triple": "",
            "bin_name": bin_name,
            "cargo_workspace_dir": os.path.join(workflow_dir, "firmware", "build"),
            "canonical_elf_relpath": "",
            "build_attempted": False,
            "build_succeeded": False,
            "elf_exists": False,
            "stdout_tail": "",
            "stderr_tail": "Missing target_triple in state/toolchain.",
        })
        return state

    prompt = f"""USER SPEC:
{spec_text}

GOAL:
{goal}

REGISTER MAP (preferred if available):
{regmap_text if regmap_text else "(not available)"}

HAL REGISTER LAYER (preferred if available):
{hal_text if hal_text else "(not available)"}

DRIVER LAYER (preferred if available):
{driver_text if driver_text else "(not available)"}

TOOLCHAIN (for future extensibility):
{json.dumps(toolchain, indent=2)}

TOGGLES:
{json.dumps(toggles, indent=2)}

TASK:
Generate a minimal, buildable embedded Rust workspace + build steps for producing an ELF.

MANDATORY:
- Prefer REGISTER MAP / HAL / DRIVER artifacts when they are available.
- Fall back to USER SPEC when those artifacts are not available.
- Assume a no_std firmware workspace (crate attributes belong ONLY in crate root).
- Include linker script reference (memory.x) and cargo target config example.
- DO NOT hardcode a CPU family (no "riscv", no "cortex", no "thumb") unless explicitly present in USER SPEC or TOOLCHAIN.
- TOOLCHAIN target triple is required and must be used exactly.
- Do NOT emit <TARGET_TRIPLE> or <BIN_NAME> placeholders.
- Use the resolved target triple and bin name exactly in all generated files.


OUTPUT REQUIREMENTS:
- build_instructions.md MUST be plain markdown (no outer ``` fences).
- build_instructions.md MUST include:
  1) exact cargo command(s) to build an ELF (including --release and --target)
  2) expected build artifact path:
   - default cargo output: target/<TARGET_TRIPLE>/release/<BIN_NAME>
   - optional: a post-step that copies/renames to <BIN_NAME>.elf (explicit command)
  3) how to confirm the ELF exists (ls/path check)
- If information is missing, add assumptions ONLY as markdown comments at top:
  <!-- ASSUMPTION: ... -->
panic.rs must contain the ONLY panic_handler.
main.rs must NOT define panic_handler.
main.rs must include: mod panic;
- Cargo.toml MUST NOT contain [build] section.
- Target configuration must be generated in: .cargo/config.toml

OUTPUTS (generate ALL using the format below):
Return multiple files in this exact format:

FILE: firmware/build/build_instructions.md
<content>

FILE: firmware/build/Cargo.toml
<content>

FILE: firmware/build/.cargo/config.toml
<content>

FILE: firmware/build/memory.x
<content>

FILE: firmware/src/main.rs
<content>

FILE: firmware/src/panic.rs
<content>
"""

 
    out = llm_chat(
        prompt,
        system="You are a senior embedded firmware engineer. Output ONLY the requested files. Never use markdown fences. No filler."
    ).strip()
    if not out:
        out = "ERROR: LLM returned empty output."

    out = strip_markdown_fences_for_code(out)

    # Parse FILE: blocks
    files = {}
    current = None
    buf = []
    for line in out.splitlines():
        if line.startswith("FILE: "):
            if current:
                files[current] = "\n".join(buf).strip() + "\n"
            current = line.replace("FILE: ", "").strip()
            buf = []
        else:
            buf.append(line)
    if current:
        files[current] = "\n".join(buf).strip() + "\n"

    # After writing main.rs
    if "mod panic;" not in files.get(OUTPUT_LIB_RS, ""):
        files[OUTPUT_LIB_RS] = "mod panic;\n" + files[OUTPUT_LIB_RS]

    # Ensure panic.rs does not contain crate-level attributes
    if OUTPUT_PANIC_RS in files:
        content = files[OUTPUT_PANIC_RS]
        content = content.replace("#![no_std]", "")
        content = content.replace("#![no_main]", "")
        files[OUTPUT_PANIC_RS] = content

    # Ensure main.rs has crate-level attrs

    # Ensure main.rs has sane embedded crate-level attrs and entrypoint

    if OUTPUT_LIB_RS in files:
        content = (files[OUTPUT_LIB_RS] or "").strip()
    else:
        content = ""

    suspicious = (
        not content
        or 'pub extern "C" pub extern "C"' in content
        or 'fn main(' in content
        or '_start' not in content
    )

    if suspicious:
        files[OUTPUT_LIB_RS] = """#![no_std]
#![no_main]

mod panic;

#[no_mangle]
pub extern "C" fn _start() -> ! {
    loop {}
}
"""
    else:
        # Minimal cleanup only if content already looks sane
        lines = [ln for ln in content.splitlines() if ln.strip()]

        cleaned = "\n".join(lines)

        if "#![no_std]" not in cleaned:
            cleaned = "#![no_std]\n" + cleaned
        if "#![no_main]" not in cleaned:
            cleaned = "#![no_main]\n" + cleaned
        if "mod panic;" not in cleaned:
            cleaned = cleaned.replace("#![no_main]\n", "#![no_main]\n\nmod panic;\n", 1)

        cleaned = cleaned.replace('pub extern "C" pub extern "C"', 'pub extern "C"')

        if "#[no_mangle]" not in cleaned and 'pub extern "C" fn _start()' in cleaned:
            cleaned = cleaned.replace(
                'pub extern "C" fn _start()',
                '#[no_mangle]\npub extern "C" fn _start()',
                1
            )

        if "loop {}" not in cleaned:
            cleaned = """#![no_std]
#![no_main]

mod panic;

#[no_mangle]
pub extern "C" fn _start() -> ! {
    loop {}
}
"""

        files[OUTPUT_LIB_RS] = cleaned.strip() + "\n"

    
    # --- Hardening: sanitize Cargo.toml (keep Embedded_Run stable) ---
    if OUTPUT_CARGO_TOML in files:
        ct = files[OUTPUT_CARGO_TOML]
        # Remove Rust crate attributes accidentally emitted into TOML
        ct = ct.replace("#![no_std]\n", "")
        ct = ct.replace("#![no_main]\n", "")

        # 1) Remove any [build] section (belongs in .cargo/config.toml)
        lines = ct.splitlines()
        out_lines = []
        in_build = False
        for ln in lines:
            s = ln.strip()
            if s.startswith("[build]"):
                in_build = True
                continue
            # end build section at next table header
            if in_build and s.startswith("[") and s.endswith("]") and not s.startswith("[build]"):
                in_build = False
            if not in_build:
                out_lines.append(ln)
        ct = "\n".join(out_lines).strip() + "\n"

        # 2) Remove fake dependency no_std (attribute, not a crate)
        ct = ct.replace('no_std = "0.1.0"\n', "")
        ct = ct.replace('no_std = "0.1"\n', "")

        # 2b) Remove bogus [no-std] table and fake core dependency
        lines = ct.splitlines()
        cleaned_lines = []
        in_no_std = False
        for ln in lines:
            s = ln.strip()
            if s == "[no-std]":
                in_no_std = True
                continue
            if in_no_std and s.startswith("[") and s.endswith("]") and s != "[no-std]":
                in_no_std = False
            if not in_no_std and 'core = "0.0.0"' not in s and 'core="0.0.0"' not in s:
                cleaned_lines.append(ln)
        ct = "\n".join(cleaned_lines).strip() + "\n"

        # 3) Ensure package name is valid (no <BIN_NAME>)
        # If model used placeholder, default to a safe name.
        ct_lines = ct.splitlines()
        new_ct_lines = []
        for ln in ct_lines:
            if ln.strip().startswith("name") and ("<" in ln or ">" in ln):
                new_ct_lines.append('name = "firmware_app"')
            else:
                new_ct_lines.append(ln)
        ct = "\n".join(new_ct_lines).strip() + "\n"

        if '[[bin]]' not in ct:
            ct += f'\n[[bin]]\nname = "{bin_name}"\npath = "../src/main.rs"\n'



        files[OUTPUT_CARGO_TOML] = ct

    # Synthesize a safe default .cargo/config.toml if the model omitted it
    resolved_target_triple = (
        toolchain.get("target_triple")
        or state.get("target_triple")
        or ""
    )

    if OUTPUT_CARGO_CFG not in files:
        files[OUTPUT_CARGO_CFG] = f"""[build]
target = "{resolved_target_triple}"
"""

    # Clean suspicious unstable/no_std patterns from config and Cargo
    if OUTPUT_CARGO_CFG in files:
        cfg = files[OUTPUT_CARGO_CFG]
        cfg = cfg.replace('[unstable]\nfeatures = ["no_std"]\n', "")
        files[OUTPUT_CARGO_CFG] = cfg

    if OUTPUT_CARGO_TOML in files:
        ct = files[OUTPUT_CARGO_TOML]
        ct = ct.replace('[unstable]\nfeatures = ["no_std"]\n', "")
        files[OUTPUT_CARGO_TOML] = ct

    # --- NEW: deterministically overwrite build instructions ---
    resolved_bin_name = (
        toolchain.get("bin_name")
        or state.get("firmware_bin_name")
        or "firmware_app"
    ).strip()

    files[OUTPUT_PATH] = f"""<!-- ASSUMPTION: Build executed inside the ChipLoop Docker image -->
<!-- ASSUMPTION: Cargo, target toolchain, and required simulation tools are already installed -->

# Build Instructions

## Build ELF

cargo build --release --target {resolved_target_triple}

## Expected Cargo Output

target/{resolved_target_triple}/release/{resolved_bin_name}

## Optional Canonical ELF Copy

mkdir -p firmware/build/target/{resolved_target_triple}/release
cp target/{resolved_target_triple}/release/{resolved_bin_name} firmware/build/target/{resolved_target_triple}/release/{resolved_bin_name}.elf

## Validate ELF Exists

ls firmware/build/target/{resolved_target_triple}/release/{resolved_bin_name}.elf
"""


    required = [
        OUTPUT_PATH,
        OUTPUT_CARGO_TOML,
        OUTPUT_CARGO_CFG,
        OUTPUT_MEMORY_X,
        OUTPUT_LIB_RS,
        OUTPUT_PANIC_RS,
    ]

    if all(p in files for p in required):
        for p in required:
            write_artifact(state, p, files[p], key=p.split("/")[-1])
    else:
        write_artifact(state, OUTPUT_PATH, out, key=OUTPUT_PATH.split("/")[-1])

    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH

    

    resolved_target_triple = (
        toolchain.get("target_triple")
        or state.get("target_triple")
        or ""
    ).strip()

    bin_name = (
        toolchain.get("bin_name")
        or state.get("firmware_bin_name")
        or "firmware_app"
    ).strip()

    if not resolved_target_triple:
        state["firmware_elf_path"] = ""
        state["firmware_expected_elf_path"] = ""
        state["elf_path"] = ""
        state["embedded_elf_path"] = ""
        state["firmware_elf_exists"] = False

        build_block = state.setdefault("firmware_build", {})
        build_block["target_triple"] = ""
        build_block["bin_name"] = bin_name
        build_block["elf_path"] = ""
        build_block["elf_exists"] = False
        build_block["build_attempted"] = False
        build_block["build_succeeded"] = False
        build_block["build_stdout"] = ""
        build_block["build_stderr"] = "Missing concrete target_triple; refusing to publish placeholder ELF."
        build_block["build_instructions_path"] = OUTPUT_PATH
        return state

    # Hard-normalize config.toml so it cannot keep unresolved placeholders
    cfg_rel = OUTPUT_CARGO_CFG
    cfg_abs = os.path.join(workflow_dir, cfg_rel)
    if workflow_dir:
        os.makedirs(os.path.dirname(cfg_abs), exist_ok=True)
        with open(cfg_abs, "w", encoding="utf-8") as f:
            f.write(f'[build]\ntarget = "{resolved_target_triple}"\n')

    # Canonical artifact path we want downstream to consume
    elf_relpath = f"firmware/build/target/{resolved_target_triple}/release/{bin_name}.elf"

    cargo_workspace_dir = os.path.join(workflow_dir, "firmware", "build")
    required_srcs = [
        os.path.join(workflow_dir, "firmware", "src", "main.rs"),
        os.path.join(workflow_dir, "firmware", "src", "panic.rs"),
        os.path.join(workflow_dir, "firmware", "build", "Cargo.toml"),
        os.path.join(workflow_dir, "firmware", "build", ".cargo", "config.toml"),
    ]

    missing_required = [p for p in required_srcs if not os.path.isfile(p)]
    missing_required_rel = [
        os.path.relpath(p, workflow_dir).replace("\\", "/")
        for p in missing_required
    ] if workflow_dir else missing_required
    if missing_required:
        build_block = state.setdefault("firmware_build", {})
        build_block["missing_required_files"] = missing_required_rel
        state["firmware_elf_exists"] = False
        state["status"] = "⚠️ ELF build blocked: required firmware build files missing"

        _write_build_result(state, {
            "agent": AGENT_NAME,
            "target_triple": resolved_target_triple,
            "bin_name": bin_name,
            "cargo_workspace_dir": cargo_workspace_dir,
            "canonical_elf_relpath": f"firmware/build/target/{resolved_target_triple}/release/{bin_name}.elf",
            "build_attempted": False,
            "build_succeeded": False,
            "missing_required_files": missing_required,
            "stdout_tail": "",
            "stderr_tail": "Required firmware build files missing before cargo build.",
        })
        return state
    cargo_target_abs = os.path.join(cargo_workspace_dir, "target", resolved_target_triple, "release", bin_name)

    build_attempted = False
    build_succeeded = False
    build_stdout = ""
    build_stderr = ""

    cargo_toml_abs = os.path.join(workflow_dir, OUTPUT_CARGO_TOML)

    # Guarded real build attempt
    cargo_path = None
    for cand in ("cargo", "/root/.cargo/bin/cargo", os.path.expanduser("~/.cargo/bin/cargo")):
        if os.path.isfile(cand) or cand == "cargo":
            cargo_path = cand
            break

    if workflow_dir and os.path.isdir(cargo_workspace_dir):
        try:
            import shutil
            import subprocess

            resolved_cargo = shutil.which(cargo_path) if cargo_path == "cargo" else cargo_path
            if resolved_cargo:
                build_attempted = True
                proc = subprocess.run(
                    [resolved_cargo, "build", "--release", "--target", resolved_target_triple],
                    cwd=cargo_workspace_dir,
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
                build_stdout = proc.stdout or ""
                build_stderr = proc.stderr or ""
                build_succeeded = (proc.returncode == 0 and os.path.isfile(cargo_target_abs))
        except Exception as e:
            build_stderr = str(e)

  

    # If Cargo built a real binary, copy it to the canonical ELF artifact path expected downstream
    elf_abs = os.path.join(workflow_dir, elf_relpath)
    if build_succeeded:
        try:
            os.makedirs(os.path.dirname(elf_abs), exist_ok=True)
            import shutil
            shutil.copy2(cargo_target_abs, elf_abs)
        except Exception as e:
            build_stderr = (build_stderr + "\n" + str(e)).strip()
            build_succeeded = False

    elf_exists = os.path.isfile(elf_abs)

    state["firmware_elf_path"] = elf_relpath
    state["firmware_expected_elf_path"] = elf_relpath
    state["elf_path"] = elf_relpath
    state["embedded_elf_path"] = elf_relpath
    state["firmware_elf_exists"] = elf_exists

    if not elf_exists:
        state["firmware_elf_path"] = elf_relpath
        state["firmware_expected_elf_path"] = elf_relpath
        state["elf_path"] = elf_relpath
        state["embedded_elf_path"] = elf_relpath
        state["firmware_elf_exists"] = False
        build_block = state.setdefault("firmware_build", {})
        build_block["target_triple"] = resolved_target_triple
        build_block["bin_name"] = bin_name
        build_block["elf_path"] = elf_relpath
        build_block["elf_exists"] = False
        build_block["build_attempted"] = build_attempted
        build_block["build_succeeded"] = build_succeeded
        build_block["cargo_target_abs"] = cargo_target_abs
        build_block["build_stdout"] = build_stdout[-4000:]
        build_block["build_stderr"] = build_stderr[-4000:]
        build_block["build_instructions_path"] = OUTPUT_PATH
        state["status"] = f"⚠️ ELF not produced at canonical path: {elf_relpath}"
        _write_build_result(state, {
            "agent": AGENT_NAME,
            "target_triple": resolved_target_triple,
            "bin_name": bin_name,
            "cargo_workspace_dir": cargo_workspace_dir,
            "cargo_target_abs": cargo_target_abs,
            "canonical_elf_relpath": elf_relpath,
            "build_attempted": build_attempted,
            "build_succeeded": build_succeeded,
            "elf_exists": False,
            "stdout_tail": build_stdout[-4000:],
            "stderr_tail": build_stderr[-4000:],
        })
        return state


    build_block = state.setdefault("firmware_build", {})
    build_block["target_triple"] = resolved_target_triple
    build_block["bin_name"] = bin_name
    build_block["elf_path"] = elf_relpath
    build_block["elf_exists"] = elf_exists
    build_block["build_attempted"] = build_attempted
    build_block["build_succeeded"] = build_succeeded
    build_block["cargo_target_abs"] = cargo_target_abs
    build_block["build_stdout"] = build_stdout[-4000:]
    build_block["build_stderr"] = build_stderr[-4000:]
    build_block["build_instructions_path"] = OUTPUT_PATH

    _write_build_result(state, {
        "agent": AGENT_NAME,
        "target_triple": resolved_target_triple,
        "bin_name": bin_name,
        "cargo_workspace_dir": cargo_workspace_dir,
        "cargo_target_abs": cargo_target_abs,
        "canonical_elf_relpath": elf_relpath,
        "build_attempted": build_attempted,
        "build_succeeded": build_succeeded,
        "elf_exists": elf_exists,
        "stdout_tail": build_stdout[-4000:],
        "stderr_tail": build_stderr[-4000:],
    })

    return state

    
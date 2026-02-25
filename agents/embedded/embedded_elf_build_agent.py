import json
from ._embedded_common import ensure_workflow_dir, llm_chat, write_artifact, strip_markdown_fences_for_code


AGENT_NAME = "Embedded ELF Build Agent"
PHASE = "elf_build"

# IMPORTANT: no trailing spaces
OUTPUT_PATH = "firmware/build/build_instructions.md"

# Workspace outputs (merged “firmware workspace agent” into this agent)
OUTPUT_CARGO_TOML = "firmware/build/Cargo.toml"
OUTPUT_CARGO_CFG  = "firmware/build/.cargo/config.toml"
OUTPUT_MEMORY_X   = "firmware/build/memory.x"
OUTPUT_LIB_RS     = "firmware/src/lib.rs"
OUTPUT_PANIC_RS   = "firmware/src/panic.rs"


def run_agent(state: dict) -> dict:
    print(f"\n🚀 Running {AGENT_NAME}...")
    ensure_workflow_dir(state)

    spec_text = (state.get("spec_text") or state.get("spec") or "").strip()
    goal = (state.get("goal") or "").strip()
    toolchain = state.get("toolchain") or {}
    toggles = state.get("toggles") or {}

    prompt = f"""USER SPEC:
{spec_text}

GOAL:
{goal}

TOOLCHAIN (for future extensibility):
{json.dumps(toolchain, indent=2)}

TOGGLES:
{json.dumps(toggles, indent=2)}


TASK:
Generate a minimal, buildable embedded Rust workspace + build steps for producing an ELF.

MANDATORY:
- Assume a no_std firmware workspace (crate attributes belong ONLY in crate root).
- Include linker script reference (memory.x) and cargo target config example.
- Provide build instructions for at least one common target triple (e.g., riscv32imac-unknown-none-elf OR thumbv7em-none-eabihf).

OUTPUT REQUIREMENTS:
- build_instructions.md MUST be plain markdown (no outer ``` fences).
- If information is missing, add assumptions ONLY as markdown comments at top:
  <!-- ASSUMPTION: ... -->

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

FILE: firmware/src/lib.rs
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
        # fallback: at least write build instructions
        write_artifact(state, OUTPUT_PATH, out, key=OUTPUT_PATH.split("/")[-1])


    # lightweight state update for downstream agents
    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH

    return state

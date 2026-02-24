import json


from ._embedded_common import (
    ensure_workflow_dir,
    llm_chat,
    write_artifact,
    strip_outer_markdown_fences,
)

AGENT_NAME = "Embedded Firmware Executive Summary Agent"
PHASE = "executive"
OUTPUT_PATH = "firmware/executive_summary.md"

def _collect_known_artifacts(state: dict) -> list[str]:
    """
    Minimal: use state['embedded'] which agents already populate.
    """
    embedded = state.get("embedded") or {}
    paths = []

    for v in embedded.values():
        if isinstance(v, str) and v.strip():
            paths.append(v.strip())
        elif isinstance(v, (list, tuple)):
            for p in v:
                if isinstance(p, str) and p.strip():
                   paths.append(p.strip())

    # de-dup preserve order
    seen = set()
    out = []
    for p in paths:
        if p not in seen:
            out.append(p)
            seen.add(p)
    return out

def run_agent(state: dict) -> dict:
    print(f"\n🚀 Running {AGENT_NAME}...")
    ensure_workflow_dir(state)

    spec_text = (state.get("spec_text") or state.get("spec") or "").strip()
    goal = (state.get("goal") or "").strip()
    toggles = state.get("toggles") or {}

    produced = _collect_known_artifacts(state)
    produced_block = "\n".join(f"- {p}" for p in produced) if produced else "- (none recorded in state['embedded'])"

    prompt = f"""USER SPEC:
{spec_text}

GOAL:
{goal}

TOGGLES:
{json.dumps(toggles, indent=2)}

KNOWN PRODUCED ARTIFACT PATHS (MUST USE EXACTLY; DO NOT INVENT NEW FILES):
{produced_block}

TASK:
Write an executive summary for this workflow run.

HARD OUTPUT RULES (IMPORTANT):
- Include these sections in this exact order:
  1) Overview (5-8 bullets max)
  2) Artifacts produced (list EXACTLY the provided paths; Do not add any. Do not omit any)
  3) Key assumptions (bullets; keep short)
  4) Risks / Gaps (bullets; actionable)
  5) Next verification steps (bullets; concrete)
 - Do NOT mention Verilator/Cocotb unless they are explicitly in the produced artifacts list.
- Keep to ~1 page.

OUTPUT PATH:
- firmware/executive_summary.md
"""

    out = llm_chat(
        prompt,
        system="You are a staff firmware lead writing production-quality executive summaries. Be specific. No filler. No hallucinated artifacts."
    ).strip()

    if not out:
        out = "ERROR: LLM returned empty output."
    # remove outer ```markdown fences if present
    out = strip_outer_markdown_fences(out)
    write_artifact(state, OUTPUT_PATH, out, key=OUTPUT_PATH.split("/")[-1])

    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH

    return state
from .fpga_common import fpga_dir, manifest_update, publish_json, read_text


def _find_actions(summary: dict, log_tail: str, state: dict) -> list[str]:
    status = str(summary.get("status") or "").lower()
    text = f"{summary.get('error') or ''}\n{log_tail}".lower()
    actions: list[str] = []
    if status == "completed":
        return actions
    if "syntax error" in text or "parse error" in text:
        actions.append("Fix Verilog/SystemVerilog syntax before rerunning synthesis.")
    if "module" in text and ("not found" in text or "can't find" in text):
        actions.append("Check top_module and ensure all dependent RTL files are included.")
    if "unsupported" in text or "systemverilog" in text:
        actions.append("Use Yosys-compatible synthesizable RTL or pre-convert unsupported constructs.")
    if state.get("allow_yosys_flatten") and not state.get("fpga_yosys_flatten"):
        actions.append("Retry Yosys with hierarchy flattening enabled.")
    if not actions:
        actions.append("Inspect yosys_synth.log and update RTL, top module, or source file list.")
    return actions


def run_agent(state: dict) -> dict:
    agent = "FPGA Synthesis Closure Agent"
    fpga = state.get("fpga") if isinstance(state.get("fpga"), dict) else {}
    out_dir = fpga_dir(state, "closure")
    synth = fpga.get("synthesis") if isinstance(fpga.get("synthesis"), dict) else {}
    command = synth.get("command") if isinstance(synth.get("command"), dict) else {}
    log_tail = str(command.get("stdout_tail") or "")
    if command.get("log"):
        log_tail = read_text(str(command.get("log")))[-4000:] or log_tail
    iteration = int(state.get("fpga_synthesis_closure_iteration_index") or 0)
    complete = synth.get("status") == "completed" and bool(fpga.get("yosys_json"))
    actions = _find_actions(synth, log_tail, state)
    if state.get("allow_yosys_flatten") and not complete:
        state["fpga_yosys_flatten"] = True
    plan = {
        "agent": agent,
        "status": "clean" if complete else "repair_recommended",
        "closure_complete": complete,
        "iteration": iteration,
        "synthesis_status": synth.get("status") or "not_run",
        "selected_restart_stage": None if complete else "FPGA Yosys Synthesis Agent",
        "actions": actions,
        "settings_for_next_iteration": {
            "fpga_yosys_flatten": bool(state.get("fpga_yosys_flatten")),
        },
    }
    publish_json(state, agent, "closure", "fpga_synthesis_closure_plan.json", plan)
    manifest_update(state, "synthesis_closure", {"plan": plan})
    return state

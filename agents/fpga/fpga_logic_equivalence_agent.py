import os
import re

from .fpga_common import board_config, fpga_dir, manifest_update, publish_json, run_cmd, write_text

AGENT_NAME = "FPGA RTL-to-Netlist Equivalence Agent"


def _progress(state: dict, message: str) -> None:
    callback = state.get("_progress_callback")
    if callable(callback):
        try:
            callback(message)
        except Exception:
            pass


def _library_reads(family: str) -> list[str]:
    return {
        "ice40": ["read_verilog -sv +/ice40/cells_sim.v"],
        "ecp5": ["read_verilog -sv +/ecp5/cells_sim.v", "read_verilog -sv +/ecp5/cells_bb.v"],
        "nexus": ["read_verilog -sv +/nexus/cells_sim.v", "read_verilog -sv +/nexus/cells_xtra.v"],
        "gowin": ["read_verilog -sv +/gowin/cells_sim.v", "read_verilog -sv +/gowin/cells_xtra.v"],
    }.get(family, [])


def _induction_depths(depth: int) -> list[int]:
    # Synthesis LEC proves a transformation of the same sequential machine; it
    # is not an unbounded functional-property proof. Run one proof at the
    # configured depth: silently clipping a requested depth to four made the
    # summary claim depth 12 while only attempting four cycles, leaving small
    # sequential cones incorrectly classified as non-equivalent.
    return [max(1, depth)]


def _proof_timeout_seconds(state: dict, *, technology_mapped: bool = False) -> int:
    explicit = state.get("fpga_lec_timeout_seconds")
    if explicit not in {None, ""}:
        return max(60, min(int(explicit), 3600))
    fpga = state.get("fpga") if isinstance(state.get("fpga"), dict) else {}
    synthesis = fpga.get("synthesis") if isinstance(fpga.get("synthesis"), dict) else {}
    flip_flops = max(0, int(synthesis.get("flip_flops") or 0))
    mapped_cells = max(0, int(synthesis.get("total_mapped_cells") or 0))
    # DigitalOcean's two-vCPU worker and Yosys 0.33 are substantially slower
    # than a current workstation. Scale from measured proof complexity rather
    # than applying the old fixed 180-second ceiling to every design.
    estimated = 180 + round(flip_flops * 0.15) + round(mapped_cells * 0.03)
    if technology_mapped:
        # The second proof loads the FPGA primitive simulation library and
        # compares the generic checkpoint with a primitive-expanded netlist.
        # Its SAT/structural workload is materially larger than RTL->generic,
        # even though both represent the same state count.  Give that proof a
        # size-derived worker budget instead of reusing the generic timeout.
        # A two-vCPU production worker must be allowed to finish the same
        # primitive-expanded proof that place-and-route may spend 20 minutes
        # on. Give mapped LEC the full automatic 30-minute production budget;
        # this changes only execution time, never acceptance criteria.
        return 1800
    return max(180, min(estimated, 1200))


def _proof_script(rtl_files: list[str], netlist: str, top: str, family: str, depths: list[int],
                  blackbox_modules: list[str] | None = None) -> str:
    blackboxes = [name for name in (blackbox_modules or []) if name and name != top]
    normalize = [
        *(f"blackbox {name}" for name in blackboxes),
        f"prep -flatten -top {top}",
        "async2sync",
        # Normalize inferred memories on both sides before equiv_make. The
        # generic synthesis checkpoint is emitted after Yosys' full memory
        # lowering pass; comparing it to unlowered RTL otherwise creates
        # independent unconstrained read-state registers and leaves one
        # unproven point per data bit despite an identical memory machine.
        "memory",
        "opt_clean",
        # Do not turn procedural next-value helper nets into formal cut
        # points. Synthesis may legally simplify their values for unreachable
        # state encodings even though all registers and observable outputs are
        # equivalent. Hiding these names from equiv_make is a generic
        # construction rule; ports are never hidden by Yosys rename -hide.
        "rename -hide w:*_next",
        "rename -hide w:next_*",
    ]
    lines = [*(f"read_verilog -sv {path}" for path in rtl_files), *normalize, f"rename {top} gold", "design -stash gold", "design -reset"]
    lines.extend(_library_reads(family))
    lines.extend([
        f"read_verilog -sv {netlist}",
        *normalize,
        f"rename {top} gate",
        "design -stash gate",
        "design -reset",
        "design -copy-from gold gold",
        "design -copy-from gate *",
        "equiv_make gold gate equiv",
        "hierarchy -top equiv",
        # FPGA netlists often encode power-up state in technology primitive
        # attributes while the source uses Verilog ``initial`` assignments.
        # Treat unknown state bits consistently during sequential proof, as
        # the ASIC LEC flow already does, instead of reporting false
        # non-equivalence solely from representation-specific X semantics.
        # First collapse matching synthesis structure, then use small SAT
        # cones.  Running a 20-cycle SAT proof over wide SPI shift registers
        # and multiple clocks scales exponentially and used to time out even
        # when synthesis was clean.
        "equiv_struct",
        "equiv_simple -undef -short",
    ])
    for depth in depths:
        lines.append(f"equiv_induct -undef -seq {depth}")
    lines.append("equiv_status -assert")
    return "\n".join(lines) + "\n"


def _unproven_points(log: str, proven: bool) -> int | None:
    if proven:
        return 0
    matches = re.findall(r"(\d+) unproven \$equiv cells", log, re.IGNORECASE)
    return int(matches[-1]) if matches else None


def _run_proof(state: dict, out_dir: str, name: str, gold_files: list[str], gate_netlist: str,
               top: str, family: str, depths: list[int], *, technology_mapped: bool = False,
               blackbox_modules: list[str] | None = None, timeout_override: int | None = None) -> dict:
    script_path = os.path.abspath(os.path.join(out_dir, f"{name}.ys"))
    log_path = os.path.abspath(os.path.join(out_dir, f"{name}.log"))
    write_text(script_path, _proof_script(gold_files, gate_netlist, top, family, depths, blackbox_modules))
    timeout_seconds = int(timeout_override or _proof_timeout_seconds(state, technology_mapped=technology_mapped))
    result = run_cmd(["yosys", "-s", script_path], cwd=out_dir, log_path=log_path, timeout=timeout_seconds, state=state)
    log = open(log_path, "r", encoding="utf-8", errors="ignore").read() if os.path.exists(log_path) else ""
    proven = bool(result.get("ok"))
    unproven = _unproven_points(log, proven)
    result_error = str(result.get("error") or "")
    timed_out = bool(re.search(r"timed out after|timeout", result_error, re.IGNORECASE))
    status = "pass" if proven else "inconclusive" if (unproven is not None or timed_out) else "fail"
    proof = {
        "status": status, "proven": proven, "gold": gold_files,
        "gate": gate_netlist, "script": script_path, "log": log_path,
        "command": result, "unproven_points": unproven, "timeout_seconds": timeout_seconds,
    }
    if not proven:
        proof["failure_kind"] = "resource_inconclusive" if timed_out else "proof_incomplete" if unproven is not None else "tool_error"
        proof["reason"] = (
            f"Yosys reached the {timeout_seconds}-second proof budget without a proof result."
            if timed_out else
            f"Yosys could not prove {unproven} equivalence points after induction depths "
            f"{', '.join(str(value) for value in depths)}."
            if unproven is not None else
            result.get("stderr_tail") or result.get("stdout_tail") or result.get("error") or "Yosys equivalence proof failed."
        )
    return proof


def _module_names(path: str) -> set[str]:
    names: set[str] = set()
    try:
        text = open(path, "r", encoding="utf-8", errors="ignore").read()
    except OSError:
        return names
    text = re.sub(r"/\*.*?\*/|//[^\r\n]*", "", text, flags=re.DOTALL)
    for escaped, plain in re.findall(r"\bmodule\s+(?:\\([^\s]+)|([A-Za-z_][A-Za-z0-9_$]*))", text):
        name = escaped or plain
        if name and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", name):
            names.add(name)
    return names


def _mapped_lec_strategy(state: dict, generic_netlist: str, mapped_netlist: str, top: str) -> dict:
    requested = str(state.get("fpga_mapped_lec_strategy") or "auto").strip().lower()
    requested = requested if requested in {"auto", "monolithic", "hierarchical"} else "auto"
    fpga = state.get("fpga") if isinstance(state.get("fpga"), dict) else {}
    synthesis = fpga.get("synthesis") if isinstance(fpga.get("synthesis"), dict) else {}
    cells = max(0, int(synthesis.get("total_mapped_cells") or 0))
    flip_flops = max(0, int(synthesis.get("flip_flops") or 0))
    shared = sorted((_module_names(generic_netlist) & _module_names(mapped_netlist)) - {top})
    large = cells >= max(1, int(state.get("fpga_hierarchical_lec_cell_threshold") or 4000)) or flip_flops >= 1000
    use_hierarchical = bool(shared and (requested == "hierarchical" or (requested == "auto" and large)))
    return {
        "requested": requested,
        "selected": "hierarchical" if use_hierarchical else "monolithic",
        "shared_partitions": shared,
        "mapped_cells": cells,
        "flip_flops": flip_flops,
        "large_design": large,
        "reason": "shared hierarchy and complexity threshold" if use_hierarchical else "monolithic proof selected",
    }


def _run_hierarchical_mapped_proof(state: dict, out_dir: str, generic_netlist: str, mapped_netlist: str,
                                   top: str, family: str, depths: list[int], partitions: list[str]) -> dict:
    proofs = []
    for index, module in enumerate(partitions, start=1):
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", module)
        _progress(state, f"FPGA mapped LEC partition {index}/{len(partitions)} started: {module}.")
        proof = _run_proof(
            state, out_dir, f"fpga_mapped_partition_{safe}", [generic_netlist], mapped_netlist,
            module, family, depths, technology_mapped=True, timeout_override=900,
        )
        proof["module"] = module
        proofs.append(proof)
        _progress(state, f"FPGA mapped LEC partition {index}/{len(partitions)} finished with status {proof['status']}.")
        if proof["status"] == "fail":
            break
    top_proof = None
    if len(proofs) == len(partitions) and all(proof["proven"] for proof in proofs):
        _progress(state, "FPGA mapped LEC top-level connectivity proof started.")
        top_proof = _run_proof(
            state, out_dir, "fpga_mapped_top_connectivity_lec", [generic_netlist], mapped_netlist,
            top, family, depths, technology_mapped=True, blackbox_modules=partitions, timeout_override=600,
        )
        _progress(state, f"FPGA mapped LEC top-level connectivity proof finished with status {top_proof['status']}.")
    all_proofs = [*proofs, *([top_proof] if top_proof else [])]
    proven = bool(top_proof and top_proof.get("proven") and len(proofs) == len(partitions) and all(p.get("proven") for p in proofs))
    incomplete = next((proof for proof in all_proofs if proof and proof.get("status") == "inconclusive"), None)
    failed = next((proof for proof in all_proofs if proof and proof.get("status") == "fail"), None)
    cause = failed or incomplete
    return {
        "status": "pass" if proven else "inconclusive" if incomplete else "fail",
        "proven": proven,
        "strategy": "hierarchical",
        "gold": [generic_netlist], "gate": mapped_netlist,
        "partitions": proofs, "top_connectivity": top_proof,
        "partition_count": len(partitions), "partitions_attempted": len(proofs),
        "partitions_proven": sum(1 for proof in proofs if proof.get("proven")),
        "coverage_complete": proven,
        "unproven_points": sum(int(proof.get("unproven_points") or 0) for proof in all_proofs if proof) or None,
        "failure_kind": None if proven else (cause or {}).get("failure_kind") or "coverage_incomplete",
        "reason": None if proven else (cause or {}).get("reason") or "Hierarchical proof coverage was incomplete.",
    }


def _run_hierarchical_generic_proof(state: dict, out_dir: str, rtl_files: list[str], generic_netlist: str,
                                    top: str, depths: list[int], partitions: list[str]) -> dict:
    """Prove leaf/state partitions, then top-level connectivity.

    Large hierarchical RTL can leave hundreds of internal helper/state points
    unproven in one flattened induction cone. Each partition is checked with
    the other shared partitions blackboxed, avoiding duplicate nested proofs;
    a final top proof blackboxes all proven partitions and checks integration.
    """
    proofs = []
    for index, module in enumerate(partitions, start=1):
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", module)
        _progress(state, f"FPGA generic LEC partition {index}/{len(partitions)} started: {module}.")
        proof = _run_proof(
            state, out_dir, f"fpga_generic_partition_{safe}", rtl_files, generic_netlist,
            module, "", depths,
            blackbox_modules=[candidate for candidate in partitions if candidate != module],
            timeout_override=600,
        )
        proof["module"] = module
        proofs.append(proof)
        _progress(state, f"FPGA generic LEC partition {index}/{len(partitions)} finished with status {proof['status']}.")
        if not proof.get("proven"):
            break
    top_proof = None
    if len(proofs) == len(partitions) and all(proof.get("proven") for proof in proofs):
        top_proof = _run_proof(
            state, out_dir, "fpga_generic_top_connectivity_lec", rtl_files, generic_netlist,
            top, "", depths, blackbox_modules=partitions, timeout_override=600,
        )
    proven = bool(top_proof and top_proof.get("proven") and all(proof.get("proven") for proof in proofs))
    all_proofs = [*proofs, *([top_proof] if top_proof else [])]
    cause = next((proof for proof in all_proofs if proof and not proof.get("proven")), None)
    return {
        "status": "pass" if proven else (cause or {}).get("status") or "fail",
        "proven": proven,
        "strategy": "hierarchical",
        "gold": rtl_files,
        "gate": generic_netlist,
        "partitions": proofs,
        "top_connectivity": top_proof,
        "partition_count": len(partitions),
        "partitions_attempted": len(proofs),
        "partitions_proven": sum(1 for proof in proofs if proof.get("proven")),
        "coverage_complete": proven,
        "unproven_points": sum(int(proof.get("unproven_points") or 0) for proof in all_proofs if proof) or None,
        "failure_kind": None if proven else (cause or {}).get("failure_kind") or "coverage_incomplete",
        "reason": None if proven else (cause or {}).get("reason") or "Hierarchical generic proof coverage was incomplete.",
    }


def run_agent(state: dict) -> dict:
    fpga = state.get("fpga") if isinstance(state.get("fpga"), dict) else {}
    enabled = bool(state.get("run_fpga_lec", True))
    required = bool(state.get("require_fpga_lec", True))
    top = str(fpga.get("top_module") or state.get("top_module") or "")
    rtl_files = [str(path) for path in fpga.get("rtl_files") or [] if os.path.exists(str(path))]
    synthesis = fpga.get("synthesis") if isinstance(fpga.get("synthesis"), dict) else {}
    generic_netlist = str(synthesis.get("equivalence_netlist") or fpga.get("yosys_equivalence_netlist") or "")
    mapped_netlist = str(
        synthesis.get("mapped_equivalence_netlist")
        or fpga.get("yosys_mapped_equivalence_netlist")
        or synthesis.get("verilog_netlist")
        or fpga.get("yosys_verilog_netlist")
        or ""
    )
    family = str(board_config(state).get("family") or "ice40").lower()
    depth = max(1, min(int(state.get("fpga_lec_induct_depth") or 12), 128))
    induction_depths = _induction_depths(depth)
    out_dir = fpga_dir(state, "lec")
    summary = {
        "agent": AGENT_NAME, "status": "disabled" if not enabled else "blocked",
        "enabled": enabled, "required": required, "tool": "Yosys",
        "comparison": "two_stage_rtl_generic_and_generic_mapped_equivalence", "top_module": top,
        "family": family, "rtl_file_count": len(rtl_files), "netlist": mapped_netlist or None,
        "generic_netlist": generic_netlist or None, "mapped_netlist": mapped_netlist or None,
        "induction_depth": depth, "induction_depths_attempted": induction_depths,
        "unproven_points": None,
    }
    if not enabled:
        summary["reason"] = "FPGA LEC disabled by user."
    elif (synthesis.get("status") != "completed" or not top or not rtl_files
          or not os.path.exists(generic_netlist) or not os.path.exists(mapped_netlist)):
        summary["reason"] = "LEC requires completed synthesis, source RTL, and both generic and FPGA-mapped netlists."
    else:
        mapped_strategy = _mapped_lec_strategy(state, generic_netlist, mapped_netlist, top)
        summary["mapped_lec_strategy"] = mapped_strategy
        generic_strategy = mapped_strategy["selected"]
        summary["generic_lec_strategy"] = {
            **mapped_strategy,
            "reason": (
                "shared hierarchy and complexity threshold; applied before the first proof"
                if generic_strategy == "hierarchical"
                else "monolithic proof selected"
            ),
        }
        _progress(
            state,
            f"FPGA LEC proof 1/2 started: RTL to generic synthesis netlist ({generic_strategy}).",
        )
        if generic_strategy == "hierarchical":
            generic_proof = _run_hierarchical_generic_proof(
                state, out_dir, rtl_files, generic_netlist, top,
                induction_depths, mapped_strategy["shared_partitions"],
            )
        else:
            generic_proof = _run_proof(
                state, out_dir, "fpga_rtl_to_generic_lec", rtl_files,
                generic_netlist, top, "", induction_depths,
            )
            generic_proof["strategy"] = "monolithic"
        if (
            not generic_proof.get("proven")
            and generic_proof.get("failure_kind") == "proof_incomplete"
            and mapped_strategy.get("shared_partitions")
        ):
            monolithic_attempt = generic_proof
            _progress(state, "FPGA generic LEC retrying with hierarchical partition proof.")
            generic_proof = _run_hierarchical_generic_proof(
                state, out_dir, rtl_files, generic_netlist, top,
                induction_depths, mapped_strategy["shared_partitions"],
            )
            generic_proof["monolithic_attempt"] = monolithic_attempt
        _progress(state, f"FPGA LEC proof 1/2 finished with status {generic_proof['status']}.")
        # A failed RTL-to-generic proof already blocks the chain. Do not spend
        # another full timeout proving a mapped netlist whose golden source has
        # not been established.
        if generic_proof["proven"]:
            _progress(state, f"FPGA LEC proof 2/2 started: generic to {family} mapped netlist ({mapped_strategy['selected']}).")
            if mapped_strategy["selected"] == "hierarchical":
                mapped_proof = _run_hierarchical_mapped_proof(
                    state, out_dir, generic_netlist, mapped_netlist, top, family,
                    induction_depths, mapped_strategy["shared_partitions"],
                )
            else:
                mapped_proof = _run_proof(
                    state, out_dir, "fpga_generic_to_mapped_lec", [generic_netlist],
                    mapped_netlist, top, family, induction_depths, technology_mapped=True,
                )
                mapped_proof["strategy"] = "monolithic"
            _progress(state, f"FPGA LEC proof 2/2 finished with status {mapped_proof['status']}.")
        else:
            mapped_proof = {
                "status": "blocked", "proven": False, "gold": [generic_netlist],
                "gate": mapped_netlist, "unproven_points": None,
                "failure_kind": "upstream_proof_failed",
                "reason": "Mapped-netlist LEC was not started because RTL-to-generic LEC did not pass.",
            }
            _progress(state, "FPGA LEC proof 2/2 skipped because proof 1 did not pass.")
        proven = bool(generic_proof["proven"] and mapped_proof["proven"])
        mapped_inconclusive = bool(generic_proof["proven"] and mapped_proof["status"] == "inconclusive")
        failed_proof = generic_proof if generic_proof["status"] != "pass" else mapped_proof
        unproven_points = sum(int(proof.get("unproven_points") or 0) for proof in (generic_proof, mapped_proof)) or None
        summary.update({
            "status": "pass" if proven else "inconclusive" if (mapped_inconclusive or unproven_points) else "fail",
            "gate_status": "pass" if proven else "pass_with_advisory" if mapped_inconclusive else "fail",
            "generic_lec": generic_proof, "mapped_lec": mapped_proof,
            "generic_proven": generic_proof["proven"], "mapped_proven": mapped_proof["proven"],
            "unproven_points": unproven_points,
            "proven": proven,
        })
        if not proven:
            summary.update(failure_kind=failed_proof.get("failure_kind"), reason=failed_proof.get("reason"))
    publish_json(state, AGENT_NAME, "lec", "fpga_lec_summary.json", summary)
    manifest_update(state, "lec", summary)
    state["fpga_lec"] = summary
    # The source-to-generic proof is the mandatory synthesis transformation
    # gate.  A technology-primitive proof is still executed and reported, but
    # an explicitly inconclusive result is advisory: Yosys may be unable to
    # model every multi-clock FPGA primitive. Real tool errors and upstream
    # proof failures remain blocking.
    if required and enabled and summary.get("gate_status", summary["status"]) not in {"pass", "pass_with_advisory"}:
        raise RuntimeError(f"FPGA RTL-to-netlist LEC did not pass: {summary.get('reason') or summary['status']}")
    return state

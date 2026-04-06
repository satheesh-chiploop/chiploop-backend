# backend/agent_capabilities.py
#
# NOTE:
# - This file is used by the planner/router to decide which agent can satisfy a task.
# - Keep 'outputs' aligned with what each agent actually uploads into Supabase Storage.
# - Use glob-like patterns where filenames are dynamic (e.g., <module>_spec.json).
#
# V&V (Cocotb/Verilator/SymbiYosys) agents added:
# - Testbench Generator Agent
# - Functional Coverage Agent
# - Formal Verification Agent
# - Simulation Control Agent
# - Bug Localization Agent
# - Golden Model Comparison Agent

AGENT_CAPABILITIES = {
    # -------------------------
    # DIGITAL: Spec -> Arch -> uArch -> RegMap -> RTL
    # -------------------------
    "Digital Spec Agent": {
        "domain": "digital",
        "inputs": [],
        # Your digital_spec_agent generates <module>_spec.json + one or more .v files,
        # and uploads logs/LLM raw output as artifacts.
        "outputs": ["*_spec.json", "*.v", "spec_agent_compile.log", "llm_raw_output.txt"],
        "description": "Creates a structured digital spec JSON and baseline Verilog modules from user input; includes compile/log artifacts.",
    },

    "Digital Architecture Agent": {
        "domain": "digital",
        "inputs": ["*_spec.json"],
        "outputs": ["digital_architecture.json", "digital_architecture_agent.log"],
        "description": "Derives block-level architecture: interfaces, datapaths/control paths, and high-level tradeoffs from the digital spec.",
    },

    "Digital Microarchitecture Agent": {
        "domain": "digital",
        "inputs": ["*_spec.json", "digital_architecture.json"],
        "outputs": ["digital_microarchitecture.json", "digital_microarchitecture_agent.log"],
        "description": "Refines architecture into implementable microarchitecture: FSMs, pipelines, queues, arbitration, and latency/throughput notes.",
    },

    "Digital Register Map Agent": {
        "domain": "digital",
        "inputs": ["*_spec.json", "digital_architecture.json"],
        "outputs": ["regmap.json", "regmap.md", "digital_register_map_agent.log"],
        "description": "Generates CSR/register map definitions: address map, fields, access types (RW/RO/WO), reset values, and side effects.",
    },
    
    "Digital Clock & Reset Architecture Agent": {
        "domain": "digital",
        "inputs": ["*_spec.json", "digital_architecture.json"],
        "outputs": [
            "clock_reset_arch_intent.json",
            "digital_clock_reset_arch_agent.log",
            "digital/constraints/top.sdc"
        ],
        "description": "Defines clock/reset intent: clock domains, reset strategies, CDC-aware intent, and generates top.sdc for downstream digital implementation flow.",
    },

    "Digital RTL Agent": {
        "domain": "digital",
        # Your digital_rtl_agent consumes spec + RTL and uploads compile/lint/summary artifacts.
        "inputs": ["*_spec.json", "*.v", "*.sv", "digital_architecture.json", "digital_microarchitecture.json", "regmap.json"],
        "outputs": ["rtl_agent_compile.log", "rtl_agent_lint_feedback.txt", "rtl_agent_summary.txt"],
        "description": "Validates and compiles generated Verilog RTL against the spec; provides lint feedback and summary.",
    },

    # -------------------------
    # DIGITAL: RTL Quality & Correctness
    # -------------------------
    "Digital RTL Linting Agent": {
        "domain": "digital",
        "inputs": ["*_spec.json", "*.v", "*.sv"],
        "outputs": ["rtl_lint_report.txt", "rtl_lint_agent.log"],
        "description": "Runs RTL lint-style checks: coding guideline violations, synthesizability red flags, and style issues.",
    },

    "Digital CDC Analysis Agent": {
        "domain": "digital",
        "inputs": ["*_spec.json", "*.v", "*.sv", "clock_reset_arch_intent.json"],
        "outputs": ["cdc_report.md", "cdc_findings.json", "digital_cdc_analysis_agent.log"],
        "description": "Analyzes clock-domain crossings and flags required synchronizers/handshakes using clock/reset intent when available.",
    },

    "Digital Reset Integrity Agent": {
        "domain": "digital",
        "inputs": ["*_spec.json", "*.v", "*.sv", "clock_reset_arch_intent.json"],
        "outputs": ["reset_integrity_report.md", "reset_integrity_findings.json", "digital_reset_integrity_agent.log"],
        "description": "Checks reset safety: async/sync usage patterns, deassertion concerns, reset-domain interactions, and common pitfalls.",
    },

 

    "Digital RTL Refactoring Agent": {
        "domain": "digital",
        "inputs": ["*_spec.json", "*.v", "*.sv"],
        "outputs": ["rtl_refactor_notes.md", "rtl_refactor_patch.sv", "rtl_refactoring_agent.log"],
        "description": "Improves RTL structure for readability/reuse: modularization suggestions and refactor patch scaffold (non-destructive).",
    },

    "Digital Assertions (SVA) Agent": {
        "domain": "digital",
        "inputs": ["*_spec.json", "*.v", "*.sv", "regmap.json"],
        "outputs": ["assertions.sv", "assertions_readme.md", "sva_agent.log"],
        "description": "Generates SystemVerilog Assertions (SVA) for protocols, safety checks, and basic liveness properties aligned to the spec/regmap.",
    },

    # -------------------------
    # VERIFICATION & VALIDATION (Cocotb + Verilator + SymbiYosys)
    # -------------------------
    "Digital Testbench Generator Agent": {
        "domain": "digital",
        "inputs": [
            "*_spec.json",
            "*.v",
            "*.sv",
            "digital_architecture.json",
            "digital_microarchitecture.json",
            "regmap.json",
            "clock_reset_arch_intent.json"
        ],
        "outputs": [
            "vv/tb/test_*.py",
            "vv/tb/Makefile",
            "vv/tb/README.md",
            "vv/tb/tb_generation_report.json",
            "vv/testbench_generator_agent.log",
        ],
        "description": "Generates Cocotb testbench skeletons and a Verilator-friendly Makefile using auto-discovered spec, RTL, clocks, and resets.",
        "requires": ["cocotb", "verilator"],
    },

    "Digital Functional Coverage Agent": {
        "domain": "digital",
        "inputs": ["*_spec.json", "*.v", "*.sv"],
        "outputs": [
            "vv/tb/coverage_model.py",
            "vv/tb/COVERAGE.md",
            "vv/tb/coverage_generation_report.json",
            "functional_coverage_agent.log",
        ],
        "description": "Generates lightweight functional coverage sampling for Cocotb (optionally uses cocotb-coverage) and produces coverage reports.",
        "requires": ["cocotb"],
    },

    "Digital Golden Model Comparison Agent": {
        "domain": "digital",
        "inputs": ["*_spec.json", "*.v", "*.sv", "digital_architecture.json", "digital_microarchitecture.json", "regmap.json"],
        "outputs": [
            "vv/tb/scoreboard.py",
            "vv/tb/SCOREBOARD.md",
            "vv/tb/golden_model_generation_report.json",
            "golden_model_comparison_agent.log",
        ],
        "description": "Generates a Python scoreboard + golden model stub for Cocotb; supports RTL vs reference model checks and scoreboarding.",
        "requires": ["cocotb"],
    },

    "Digital Simulation Control Agent": {
        "domain": "digital",
        "inputs": ["vv/tb/Makefile", "vv/tb/test_*.py", "*.v", "*.sv", "*_spec.json"],
        "outputs": [
            "vv/tb/run_regression.py",
            "vv/tb/SIM_CONTROL.md",
            "vv/tb/sim_control_generation_report.json",
            "simulation_control_agent.log",
        ],
        "description": "Generates regression orchestration (multi-test, multi-seed) for Cocotb+Verilator runs; seed management and JSON summary output.",
        "requires": ["make", "cocotb", "verilator"],
    },

    "Digital Simulation Execution Agent": {
        "domain": "digital",
        "inputs": [
            "vv/tb/simulation_manifest.json(optional)",
            "vv/tb/Makefile",
            "vv/tb/test_*.py",
            "vv/tb/run_regression.py(optional)",
            "*_spec.json",
            "*.v",
            "*.sv"
        ],
        "outputs": [
            "vv/tb/reports/simulation_execution_summary.json",
            "vv/tb/reports/SIM_EXECUTION.md",
            "vv/tb/reports/simulation_execution_report.json",
            "vv/tb/reports/run_logs/*.stdout.log",
            "vv/tb/reports/run_logs/*.stderr.log",
            "simulation_execution_agent.log",
        ],
        "description": "Consumes simulation manifest and upstream testcase definitions, runs make-based Cocotb/Verilator regressions across tests and seeds, and emits execution summaries plus per-run logs.",
        "requires": ["make", "cocotb", "verilator"],
    },

    "Digital Simulation Summary Coverage Agent": {
        "domain": "digital",
        "inputs": [
            "vv/tb/reports/simulation_execution_summary.json",
            "vv/tb/reports/functional_coverage_summary.json(optional)",
            "vv/tb/COVERAGE.md(optional)",
            "*.log(optional)"
        ],
        "outputs": [
            "vv/tb/reports/simulation_summary_coverage.json",
            "vv/tb/reports/SIM_SUMMARY_COVERAGE.md",
            "vv/tb/reports/simulation_summary_coverage_report.json",
            "simulation_summary_coverage_agent.log",
        ],
        "description": "Parses simulation execution outputs and functional coverage summaries and publishes a merged dashboard-style summary for UI display.",
        "requires": [],
    },

    "Digital Bug Localization Agent": {
        "domain": "digital",
        "inputs": ["simulation.log", "vv/tb/run_regression.py", "vv/tb/test_*.py"],
        "outputs": [
            "vv/tb/bug_localize.py",
            "vv/tb/BUG_LOCALIZE.md",
            "vv/tb/bug_localization_generation_report.json",
            "bug_localization_agent.log",
        ],
        "description": "Heuristic failure root-cause hints by scanning logs for assertion/mismatch signatures; points to likely first divergence.",
        "requires": [],
    },

    "Digital Formal Verification Agent": {
        "domain": "digital",
        "inputs": ["*_spec.json", "*.v", "*.sv", "assertions.sv", "clock_reset_architecture.json"],
        "outputs": [
            "vv/formal/*.sby",
            "vv/formal/*_formal.sv",
            "vv/formal/formal_report.json",
            "formal_verification_agent.log",
        ],
        "description": "Generates SymbiYosys orchestration configs and a minimal formal wrapper; optionally runs sby if available.",
        "requires": ["sby", "yosys"],
    },

     # -------------------------
    # Extended / optional digital flow agents
    # -------------------------
# -------------------------
    # SIGNOFF / HANDOFF (Open-source friendly)
    # -------------------------
    "Digital Synthesis Readiness Agent": {
        "domain": "digital",
        "inputs": ["*_spec.json", "*.v", "*.sv", "digital_architecture.json", "digital_microarchitecture.json", "clock_reset_architecture.json"],
        "outputs": [
            "signoff/synthesis_readiness_report.md",
            "signoff/synthesis_readiness_findings.json",
            "signoff/yosys_synth.log",
        ],
        "description": "Checks synthesizable subset red flags and runs open-source synthesis sanity (Yosys) to assess readiness; reports timing/area intent gaps from spec.",
        "requires": ["yosys"],
    },

    "Digital Power Intent (UPF-lite) Agent": {
        "domain": "digital",
        "inputs": ["*_spec.json", "digital_architecture.json"],
        "outputs": [
            "signoff/power_intent.upf",
            "signoff/power_intent_report.md",
            "signoff/power_intent_findings.json",
        ],
        "description": "Generates a UPF-lite power intent file (domains + optional isolation/retention) derived from spec/architecture (no hardcoding).",
        "requires": [],
    },

    "Digital IP Packaging & Handoff Agent": {
        "domain": "digital",
        "inputs": ["*_spec.json", "*.v", "*.sv", "regmap.json", "signoff/power_intent.upf", "signoff/synthesis_readiness_findings.json", "vv/**"],
        "outputs": [
            "handoff/ip_packaging_report.md",
            "handoff/ip_packaging_report.json",
            "handoff/MANIFEST.json",
            "handoff/DELIVERABLES.md",
        ],
        "description": "Creates SoC-ready IP package folder layout + manifest + checklist; bundles key RTL/docs/constraints/power/verification collateral.",
        "requires": [],
    },

    "Digital Smoke Preflight Agent": {
        "domain": "digital",
        "inputs": ["*.v", "*.sv", "*.vh", "*.svh", "*_spec.json"],
        "outputs": ["vv/smoke/smoke_preflight.json"],
        "description": "Normalizes RTL inputs for Smoke, infers top module, and writes a stable smoke_preflight.json manifest.",
        "requires": [],
    },

    "Digital Smoke Executive Summary Agent": {
        "domain": "digital",
        "inputs": ["rtl_agent_compile.log", "vv/tb/sim_control_generation_report.json", "vv/smoke/smoke_preflight.json"],
        "outputs": ["vv/smoke/SMOKE_SUMMARY.md", "vv/smoke/smoke_summary.json"],
        "description": "Creates one-page Smoke PASS/FAIL summary + next steps using compile + sim control outputs.",
        "requires": [],
    },

    "Digital RTL Signature Agent": {
        "domain": "digital",
        "inputs": ["*.v", "*.sv", "*.vh", "*.svh"],
        "outputs": ["integrate/rtl_signatures.json"],
        "description": "Scans RTL sources and extracts module/port signatures into a stable rtl_signatures.json for downstream integration planning.",
        "requires": [],
    },

    "Digital Integration Intent Agent": {
        "domain": "digital",
        "inputs": ["integrate/rtl_signatures.json", "integration_description", "top_module(optional)"],
        "outputs": ["integrate/integration_intent.json"],
        "description": "Transforms user integration description into structured integration_intent.json (instances, connections, tieoffs) using RTL signatures.",
        "requires": [],
    },

    "Digital Top Assembly Agent": {
        "domain": "digital",
        "inputs": ["integrate/integration_intent.json", "integrate/rtl_signatures.json", "top_module"],
        "outputs": ["integrate/*.sv", "integrate/top_assembly_report.md"],
        "description": "Generates top-level assembly RTL (top_module.sv) and a short assembly report from the integration intent.",
        "requires": [],
    },

    "Digital Foundry Profile Agent": {
        "domain": "digital",
        "inputs": ["workflow_id"],
        "outputs": [
           "digital/foundry/foundry_profile.json",
           "digital/foundry/foundry_profile.log"
        ],
        "description": "Creates a portable foundry profile (PDK name, PDK_ROOT, corner intent) and validates PDK presence."
    },

    "Digital Implementation Setup Agent": {
        "domain": "digital",
        "inputs": ["digital/foundry/foundry_profile.json", "*_spec.json", "*.v", "*.sv"],
        "outputs": [
           "digital/foundry/constraints/base.sdc",
           "digital/foundry/corners/corners.json",
           "digital/foundry/openlane/config.json",
           "digital/foundry/setup/implementation_setup.log"
        ],
        "description": "Generates implementation collateral (constraints + corners + OpenLane2 config) using the foundry profile. No P&R stages yet."
    },

    "Digital Synthesis Agent": {
        "domain": "digital",
        "description": (
            "Runs OpenLane2 Yosys synthesis in Docker. Consumes single-source SDC from "
            "digital/constraints/top.sdc (does not regenerate constraints). Exports "
            "stable netlist + metrics.json for downstream stages."
        ),
        "requires": ["docker"],
        "inputs": [
            "spec/*_spec.json",
            "spec/*.v",
            "spec/*.sv",
            "digital/rtl_refactored/*.v",
            "digital/constraints/top.sdc",
            "digital/foundry/openlane/config.json"
        ],
        "outputs": [
            "digital/synth/config.json",
            "digital/synth/constraints/top.sdc",
            "digital/synth/run.sh",
            "digital/synth/logs/openlane_synth.log",
            "digital/synth/synth_summary.json",
            "digital/synth/synth_summary.md",
            "digital/synth/netlist/*_synth.v",
            "digital/synth/metrics.json"
        ],
    },

    "Digital STA PrePlace Agent": {
        "domain": "digital",
        "description": (
            "Runs OpenLane2 STA pre-PnR using synthesized netlist + single-source SDC. "
            "Exports stable metrics.json for regression and executive summary parsing."
        ),
        "requires": ["docker"],
        "inputs": [
            "digital/constraints/top.sdc",
            "digital/synth/netlist/*_synth.v",
            "digital/foundry/openlane/config.json"
        ],
        "outputs": [
            "digital/sta_preplace/config.json",
            "digital/sta_preplace/constraints/top.sdc",
            "digital/sta_preplace/run.sh",
            "digital/sta_preplace/logs/openlane_sta_preplace.log",
            "digital/sta_preplace/metrics.json",
            "digital/sta_preplace/sta_summary.json",
            "digital/sta_preplace/sta_summary.md"
        ],
    },

    "Digital Floorplan Agent": {
        "domain": "digital",
        "description": (
            "Runs OpenLane2 floorplan stage (OpenROAD.Floorplan). Consumes single-source "
            "SDC and exports primary DEF + metrics.json."
        ),
        "requires": ["docker"],
        "inputs": [
            "digital/constraints/top.sdc",
            "digital/foundry/openlane/config.json",
            "digital/synth/config.json"
        ],
        "outputs": [
            "digital/floorplan/config.json",
            "digital/floorplan/constraints/top.sdc",
            "digital/floorplan/run.sh",
            "digital/floorplan/logs/openlane_floorplan.log",
            "digital/floorplan/metrics.json",
            "digital/floorplan/primary.def",
            "digital/floorplan/floorplan_summary.json",
            "digital/floorplan/floorplan_summary.md"
        ],
    },

    "Digital Placement Agent": {
        "domain": "digital",
        "description": (
            "Runs OpenLane2 placement stage (OpenROAD.DetailedPlacement). Consumes single-source "
            "SDC and exports placed DEF + metrics.json."
        ),
        "requires": ["docker"],
        "inputs": [
            "digital/constraints/top.sdc",
            "digital/foundry/openlane/config.json",
            "digital/synth/config.json"
        ],
        "outputs": [
            "digital/place/config.json",
            "digital/place/constraints/top.sdc",
            "digital/place/run.sh",
            "digital/place/logs/openlane_place.log",
            "digital/place/metrics.json",
            "digital/place/primary.def",
            "digital/place/place_summary.json",
            "digital/place/place_summary.md"
        ],
    },

    "Digital STA PostPlace Agent": {
        "domain": "digital",
        "description": (
            "Runs OpenLane2 STA after placement (mid-PnR). Consumes single-source SDC and "
            "exports stable metrics.json."
        ),
        "requires": ["docker"],
        "inputs": [
            "digital/constraints/top.sdc",
            "digital/place/primary.def",
            "digital/foundry/openlane/config.json"
        ],
        "outputs": [
            "digital/sta_postplace/config.json",
            "digital/sta_postplace/constraints/top.sdc",
            "digital/sta_postplace/run.sh",
            "digital/sta_postplace/logs/openlane_sta_postplace.log",
            "digital/sta_postplace/metrics.json",
            "digital/sta_postplace/sta_summary.json",
            "digital/sta_postplace/sta_summary.md"
        ],
    },

    "Digital CTS Agent": {
        "domain": "digital",
        "description": "Runs OpenLane2 OpenROAD.CTS. Consumes single-source SDC and exports primary DEF + metrics.json.",
        "requires": ["docker"],
        "inputs": [
            "digital/constraints/top.sdc",
            "digital/place/primary.def",
            "digital/foundry/openlane/config.json",
            "digital/synth/netlist/*_synth.v"
        ],
        "outputs": [
            "digital/cts/config.json",
            "digital/cts/constraints/top.sdc",
            "digital/cts/run.sh",
            "digital/cts/logs/openlane_cts.log",
            "digital/cts/metrics.json",
            "digital/cts/primary.def",
            "digital/cts/cts_summary.json",
            "digital/cts/cts_summary.md"
        ],
    },

    "Digital STA PostCTS Agent": {
        "domain": "digital",
        "description": "Runs OpenLane2 STA after CTS (uses OpenROAD.STAMidPNR). Exports metrics.json.",
        "requires": ["docker"],
        "inputs": [
            "digital/constraints/top.sdc",
            "digital/cts/primary.def",
            "digital/foundry/openlane/config.json"
        ],
        "outputs": [
            "digital/sta_postcts/config.json",
            "digital/sta_postcts/constraints/top.sdc",
            "digital/sta_postcts/run.sh",
            "digital/sta_postcts/logs/openlane_sta_postcts.log",
            "digital/sta_postcts/metrics.json",
            "digital/sta_postcts/sta_summary.json",
            "digital/sta_postcts/sta_summary.md"
        ],
    },

    "Digital Route Agent": {
        "domain": "digital",
        "description": "Runs OpenLane2 OpenROAD.DetailedRouting. Exports routed DEF + metrics.json.",
        "requires": ["docker"],
        "inputs": [
            "digital/constraints/top.sdc",
            "digital/cts/primary.def",
            "digital/foundry/openlane/config.json"
        ],
        "outputs": [
            "digital/route/config.json",
            "digital/route/constraints/top.sdc",
            "digital/route/run.sh",
            "digital/route/logs/openlane_route.log",
            "digital/route/metrics.json",
            "digital/route/primary.def",
            "digital/route/route_summary.json",
            "digital/route/route_summary.md"
        ],
    },

    "Digital STA PostRoute Agent": {
        "domain": "digital",
        "description": "Runs OpenLane2 signoff STA (OpenROAD.STAPostPNR). Exports metrics.json.",
        "requires": ["docker"],
        "inputs": [
            "digital/constraints/top.sdc",
            "digital/route/primary.def",
            "digital/foundry/openlane/config.json"
        ],
        "outputs": [
            "digital/sta_postroute/config.json",
            "digital/sta_postroute/constraints/top.sdc",
            "digital/sta_postroute/run.sh",
            "digital/sta_postroute/logs/openlane_sta_postroute.log",
            "digital/sta_postroute/metrics.json",
            "digital/sta_postroute/sta_summary.json",
            "digital/sta_postroute/sta_summary.md"
        ],
    },

    "Digital Fill Agent": {
        "domain": "digital",
        "description": "Runs OpenLane2 OpenROAD.FillInsertion. Exports DEF + metrics.json.",
        "requires": ["docker"],
        "inputs": [
            "digital/constraints/top.sdc",
            "digital/route/primary.def",
            "digital/foundry/openlane/config.json"
        ],
        "outputs": [
            "digital/fill/config.json",
            "digital/fill/constraints/top.sdc",
            "digital/fill/run.sh",
            "digital/fill/logs/openlane_fill.log",
            "digital/fill/metrics.json",
            "digital/fill/primary.def",
            "digital/fill/fill_summary.json",
            "digital/fill/fill_summary.md"
        ],
    },

    "Digital DRC Agent": {
        "domain": "digital",
        "description": "Runs OpenLane2 DRC (KLayout.DRC by default). Exports metrics.json + drc log summary.",
        "requires": ["docker"],
        "inputs": [
            "digital/fill/primary.def",
            "digital/foundry/openlane/config.json"
        ],
        "outputs": [
            "digital/drc/config.json",
            "digital/drc/run.sh",
            "digital/drc/logs/openlane_drc.log",
            "digital/drc/metrics.json",
            "digital/drc/drc_summary.json",
            "digital/drc/drc_summary.md"
        ],
    },

    "Digital LVS Agent": {
        "domain": "digital",
        "description": "Runs OpenLane2 LVS (Netgen.LVS). Exports metrics.json + lvs log summary.",
        "requires": ["docker"],
        "inputs": [
            "digital/fill/primary.def",
            "digital/foundry/openlane/config.json",
            "digital/synth/netlist/*_synth.v"
        ],
        "outputs": [
            "digital/lvs/config.json",
            "digital/lvs/run.sh",
            "digital/lvs/logs/openlane_lvs.log",
            "digital/lvs/metrics.json",
            "digital/lvs/lvs_summary.json",
            "digital/lvs/lvs_summary.md"
        ],
    },

    "Digital Tapeout Agent": {
        "domain": "digital",
        "description": "Runs OpenLane2 streamout to GDS (KLayout.StreamOut + Magic.StreamOut + optional KLayout.XOR). Exports GDS paths + metrics.json.",
        "requires": ["docker"],
        "inputs": [
            "digital/fill/primary.def",
            "digital/foundry/openlane/config.json"
        ],
        "outputs": [
            "digital/tapeout/config.json",
            "digital/tapeout/run.sh",
            "digital/tapeout/logs/openlane_tapeout.log",
            "digital/tapeout/metrics.json",
            "digital/tapeout/gds/klayout.gds",
            "digital/tapeout/gds/magic.gds",
            "digital/tapeout/tapeout_summary.json",
            "digital/tapeout/tapeout_summary.md"
        ],
    },

    "Digital Executive Summary Agent": {
        "domain": "digital",
        "description": "Parses stage metrics.json and produces executive_summary.md/json. No fake precision.",
        "requires": [],
        "inputs": [
            "digital/synth/metrics.json",
            "digital/sta_postroute/metrics.json",
            "digital/drc/metrics.json",
            "digital/lvs/metrics.json",
            "digital/tapeout/metrics.json"
        ],
        "outputs": [
            "digital/executive_summary.json",
            "digital/executive_summary.md"
        ],
    },

    # -------------------------
    # ANALOG
    # -------------------------
    # -------------------------
    # ANALOG (NEW, FRESH)
    # -------------------------
        # -------------------------
    # ANALOG (Production Scaffold)
    "Analog Spec Builder Agent": {
        "domain": "analog",
        "inputs": ["datasheet_text", "analog_datasheet", "spec", "spec_text", "goal"],
        "outputs": [
            "analog/spec.json",
            "analog/spec_summary.md",
            "analog/spec/spec_normalized.json",
            "analog/spec/requirements.json",
            "analog/spec/assumptions.md",
        ],
        "description": "Normalizes a free-text analog datasheet into a structured spec JSON for downstream analog generators. This is the only analog agent that interprets free-text directly.",
    },

    "Analog Netlist Scaffold Agent": {
        "domain": "analog",
        "inputs": ["analog/spec/spec_normalized.json", "analog/spec.json"],
        "outputs": [
           "analog/netlist/<block_name>_top.sp",
           "analog/netlist.sp",
           "analog/netlist_summary.md",
        ],
        "description": "Creates a spec-driven SPICE interface scaffold using the normalized analog spec. No block-type assumptions are hardcoded.",
    },



    "Analog Behavioral Model Agent": {
        "domain": "analog",
        "inputs": ["analog/spec/spec_normalized.json", "analog/spec.json"],
        "outputs": [
            "analog/model.sv",
            "analog/model_params.json",
            "analog/model_notes.md",
        ],
        "description": "Creates a deterministic behavioral SystemVerilog scaffold from the normalized analog spec, including ports and behavioral contract notes.",
    },

    "Analog Behavioral Testbench Agent": {
        "domain": "analog",
        "inputs": ["analog/spec/spec_normalized.json", "analog/spec.json", "analog/model.sv"],
        "outputs": [
            "analog/behavioral/tb_<block_name>_behavioral.sv",
            "analog/tb.sv",
        ],
        "description": "Creates a spec-driven behavioral testbench from the normalized analog spec and generated model. Signal declarations and instance port maps are derived from the spec.",
    },

    "Analog Simulation Plan Agent": {
        "domain": "analog",
        "inputs": [
            "analog/spec/spec_normalized.json",
            "analog/spec.json",
            "analog/netlist.sp",
            "analog/netlist/<block_name>_top.sp",
        ],
        "outputs": [
            "analog/sim_plan.json",
            "analog/run_deck.sp",
            "analog/sim/sim_plan.json",
            "analog/sim/env.sh",
            "analog/sim/run_all.sh",
            "analog/sim/ngspice/run_all.sh",
            "analog/sim/ngspice/decks/*.sp",
            "analog/sim/spectre/run_all.sh",
            "analog/sim/spectre/decks/*.sp",
            "analog/sim/hspice/run_all.sh",
            "analog/sim/hspice/decks/*.sp",
            "analog/sim/parse/extract_metrics.py",
            "analog/sim/results/metrics.json",
        ],
        "description": "Creates a spec-driven multi-simulator analog simulation plan, runnable deck scaffold, and stub metrics extraction without hardcoded block-type assumptions.",
    },


    "Analog Behavioral Assertions Agent": {
        "domain": "analog",
        "inputs": ["analog/spec.json", "analog/model.sv", "analog/spec/requirements.json"],
        "outputs": [
            # canonical
            "analog/behavioral/assertions.sv",
            # legacy
            "analog/sva.sv",
        ],
        "description": "Generates SV assertions/checkers for enable sequencing, limits, settling windows, etc. (canonical behavioral folder + legacy compatibility).",
    },

    "Analog Behavioral Coverage Agent": {
        "domain": "analog",
        "inputs": ["analog/spec.json"],
        "outputs": [
            # canonical
            "analog/behavioral/coverage_plan.json",
            "analog/behavioral/coverage_summary.md",

            # legacy
            "analog/coverage_plan.json",
            "analog/validation_summary.md",
        ],
        "description": "Defines scenario/corner/sweep coverage intent for analog validation (PDK-agnostic corner naming).",
    },

    "Analog Correlation Agent": {
        "domain": "analog",
        "inputs": [
            "analog/spec.json",
            # legacy plan
            "analog/sim_plan.json",
            # canonical sim metrics
            "analog/sim/results/metrics.json",
            "analog/model.sv",
        ],
        "outputs": [
            # canonical
            "analog/correlation/correlation_plan.md",
            "analog/correlation/metrics_compare.json",
            "analog/correlation/deltas.json",
            "analog/correlation/delta_summary.json",
            "analog/correlation/correlation_report.md",

            # legacy
            "analog/metrics_compare.json",
            "analog/delta_summary.json",
            "analog/correlation_report.md",
        ],
        "description": "Defines metric correlation between behavioral and netlist; produces correlation plan + deltas + report under analog/correlation/ (keeps legacy flat outputs during transition).",
    },

    "Analog Iteration Proposal Agent": {
        "domain": "analog",
        "inputs": [
            # prefer canonical delta summary, keep legacy input too
            "analog/correlation/delta_summary.json",
            "analog/delta_summary.json",
            "analog/model.sv",
        ],
        "outputs": [
            # canonical scaffold outputs
            "analog/iteration/iteration_proposal.md",
            "analog/iteration/backlog.yaml",
            "analog/iteration/next_run_plan.json",
            "analog/iteration/iteration_patch.diff",

            # legacy compatibility
            "analog/iteration_rationale.md",
            "analog/next_run_plan.json",
            "analog/iteration_patch.diff",
        ],
        "description": "Proposes tuning/code patches and re-run plan based on correlation deltas (canonical iteration folder + legacy compatibility).",
    },

    "Analog Abstract Views Agent": {
        "domain": "analog",
        "inputs": ["analog/spec.json"],
        "outputs": [
            "analog/abstract/macro.lef",
            "analog/abstract/macro_stub.lib",
            "analog/abstract/integration_notes.md",
        ],
        "description": "Generates LEF + LIB stub + integration notes for physical/timing handoff (canonical abstract/ plus legacy abstracts/ mirror).",
    },

    "Analog Executive Summary Agent": {
        "domain": "analog",
        "inputs": ["analog/spec/spec_normalized.json", "analog/sim/results/metrics.json", "analog/correlation/delta_summary.json", "*"],
        "outputs": ["analog/executive_summary.md"],
        "description": "Creates an artifact-aware executive summary with block/module identity, compliance table, correlation risks, and artifact readiness for Analog_Run and System_Sim.",
    },

    
    # -------------------------
    # EMBEDDED
    # -------------------------
    "Embedded Spec Agent": {
        "domain": "embedded",
        "inputs": ["*_spec.json", "digital_architecture.json", "regmap.json"],
        # Your embedded_spec_agent creates <firmware_name>_embedded_spec.json
        "outputs": ["*_embedded_spec.json", "embedded_spec_agent.log"],
        "description": "Generates a generic embedded integration spec (MMIO/register plan) derived from the digital spec/arch/regmap.",
    },

    "Embedded Code Agent": {
        "domain": "embedded",
        "inputs": ["*_embedded_spec.json"],
        "outputs": ["main.cpp", "embedded_code_agent.log"],
        "description": "Generates generic C++ bare-metal firmware scaffolding from the embedded spec (MMIO access, polling loop).",
    },

    # Simulation + demo result agents (planner selectable)
    "Embedded Sim Agent": {
        "domain": "embedded",
        "inputs": ["main.cpp", "*_embedded_spec.json", "*.v", "*.sv"],
        "outputs": ["telemetry.json", "simulation.log"],
        "description": "Simulates firmware behavior over time and generates structured telemetry for demos/analysis.",
    },

    "Embedded Result Agent": {
        "domain": "embedded",
        "inputs": ["telemetry.json"],
        "outputs": ["result_summary.txt"],
        "description": "Generates a short demo-ready summary report based on telemetry.",
    },

        # ✅ Embedded Firmware Loop Agents (v2)

    "Embedded Firmware Register Extract Agent": {
        "domain": "embedded",
        "inputs": [
            "digital/digital_regmap.json(optional)",
            "digital/regmap.json(optional)",
            "digital/register_map.json(optional)",
            "regmap.json(optional)",
            "system/integration/system_integration_intent.json(optional)",
            "spec_text(optional)",
            "toolchain(optional)",
            "toggles(optional)"
        ],
        "outputs": ["firmware/register_map.json"],
        "description": "Normalizes upstream register artifacts into a firmware-friendly register map; falls back to spec-driven extraction for standalone Embedded_Run and other individual loop runs.",
    },

    "Embedded Rust Register Layer Generator Agent": {
        "domain": "embedded",
        "inputs": [
            "firmware/register_map.json(optional)",
            "spec_text(optional)",
            "toolchain(optional)",
            "toggles(optional)"
        ],
        "outputs": ["firmware/hal/registers.rs"],
        "description": "Generates Rust HAL register abstractions from firmware/register_map.json when available, with standalone spec-driven fallback preserved.",
    },

    "Embedded Register Validation Agent": {
        "domain": "embedded",
        "inputs": [
            "firmware/register_map.json(optional)",
            "firmware/hal/registers.rs(optional)",
            "spec_text(optional)",
            "toolchain(optional)",
            "toggles(optional)"
        ],
        "outputs": ["firmware/hal/register_validation.md"],
        "description": "Validates register-map and HAL consistency using generated artifacts when present, while remaining compatible with standalone Embedded_Run.",
    },

    "Embedded Rust Driver Scaffold Agent": {
        "domain": "embedded",
        "inputs": [
            "firmware/register_map.json(optional)",
            "firmware/hal/registers.rs(optional)",
            "spec_text(optional)",
            "toolchain(optional)",
            "toggles(optional)"
        ],
        "outputs": ["firmware/drivers/driver_scaffold.rs"],
        "description": "Generates a Rust driver scaffold from normalized register/HAL artifacts when present, while preserving standalone spec-driven behavior.",
    },

    "Embedded ELF Build Agent": {
        "domain": "embedded",
        "inputs": [
            "firmware/register_map.json(optional)",
            "firmware/hal/registers.rs(optional)",
            "firmware/drivers/driver_scaffold.rs(optional)",
            "firmware/src/main.rs(optional)",
            "spec_text(optional)",
            "toolchain(optional)",
            "toggles(optional)",
            "system/integration/soc_top_sim.sv(optional)"
        ],
        "outputs": [
            "firmware/build/build_instructions.md",
            "firmware/build/Cargo.toml",
            "firmware/build/.cargo/config.toml",
            "firmware/build/memory.x",
            "firmware/src/main.rs",
            "firmware/src/panic.rs",
            "firmware/build/firmware.elf(optional)"
        ],
        "description": "Builds firmware ELF collateral when downstream firmware sources are available, while preserving standalone Embedded_Run behavior through spec/toolchain/toggle fallback inputs.",
    },

     
    
    "Embedded Interrupt Mapping Agent": {
        "domain": "embedded",
        "inputs": ["spec_text", "toolchain", "toggles"],
        "outputs": ["firmware/isr/interrupts.rs"],
        "description": "Create interrupt vector mapping and ISR stubs.",
    },
    "Embedded DMA Integration Agent": {
        "domain": "embedded",
        "inputs": ["spec_text", "toolchain", "toggles"],
        "outputs": ["firmware/dma/dma.rs"],
        "description": "Integrate DMA channels, descriptors, and ISR hooks.",
    },
    "Embedded Boot Dependency Planner Agent": {
        "domain": "embedded",
        "inputs": ["spec_text", "toolchain", "toggles"],
        "outputs": ["firmware/boot/boot_sequence.md"],
        "description": "Plan boot sequencing dependencies (clocks, resets, power domains).",
    },
    "Embedded Clock And PLL Configuration Agent": {
        "domain": "embedded",
        "inputs": ["spec_text", "toolchain", "toggles"],
        "outputs": ["firmware/boot/pll_config.rs"],
        "description": "Generate clock tree + PLL configuration code and notes.",
    },
    "Embedded Reset Sequencing Agent": {
        "domain": "embedded",
        "inputs": ["spec_text", "toolchain", "toggles"],
        "outputs": ["firmware/boot/reset_sequence.rs"],
        "description": "Generate reset sequencing logic and ordering.",
    },
    "Embedded Power Mode Configuration Agent": {
        "domain": "embedded",
        "inputs": ["spec_text", "toolchain", "toggles"],
        "outputs": ["firmware/power_profiles/power_modes.md"],
        "description": "Generate power mode table and transition APIs.",
    },
    "Embedded Boot Timing Validation Agent": {
        "domain": "embedded",
        "inputs": ["spec_text", "toolchain", "toggles"],
        "outputs": ["firmware/boot/boot_timing_validation.md"],
        "description": "Validate timing constraints and ordering assumptions.",
    },
    "Embedded Register Dump Utility Agent": {
        "domain": "embedded",
        "inputs": ["spec_text", "toolchain", "toggles"],
        "outputs": ["firmware/diagnostics/register_dump.rs"],
        "description": "Create register dump utility and formatting.",
    },
    "Embedded Built In Self Test Agent": {
        "domain": "embedded",
        "inputs": ["spec_text", "toolchain", "toggles"],
        "outputs": ["firmware/diagnostics/bist.rs"],
        "description": "Create BIST hooks and test routines.",
    },
    "Embedded Stress Test Generator Agent": {
        "domain": "embedded",
        "inputs": ["spec_text", "toolchain", "toggles"],
        "outputs": ["firmware/diagnostics/stress_tests.rs"],
        "description": "Create stress tests for clocks/IRQs/DMA/memory.",
    },
    "Embedded Firmware Integration Contract Agent": {
        "domain": "embedded",
        "inputs": ["spec_text", "toolchain", "toggles"],
        "outputs": ["firmware/integration_contract.md"],
        "description": "Generate integration contract: APIs, expected behaviors, interrupts, DMA, power.",
    },
    "Embedded Firmware Executive Summary Agent": {
        "domain": "embedded",
        "inputs": ["spec_text", "toolchain", "toggles"],
        "outputs": ["firmware/executive_summary.md"],
        "description": "Generate exec summary of produced firmware deliverables.",
    },
    
    "Embedded Verilator Build Agent": {
        "domain": "embedded",
        "inputs": ["spec_text", "toolchain", "toggles"],
        "outputs": ["firmware/validate/verilator_build.md"],
        "description": "Generate Verilator build instructions and flags.",
    },
    "Embedded Cocotb Harness Agent": {
        "domain": "embedded",
        "inputs": ["spec_text", "toolchain", "toggles"],
        "outputs": ["firmware/validate/cocotb_harness.py"],
        "description": "Generate cocotb harness scaffold for firmware/RTL cosim.",
    },
    "Embedded Co Sim Runner Agent": {
        "domain": "embedded",
        "inputs": ["spec_text", "toolchain", "toggles"],
        "outputs": [
        "firmware/validate/cosim_run.md",
        "firmware/validate/run_cosim.sh",
        ],
        "description": "Generate cosim runner steps and expected artifacts.",
    },
    "Embedded Coverage Collector Agent": {
        "domain": "embedded",
        "inputs": ["spec_text", "toolchain", "toggles"],
        "outputs": [
        "firmware/validate/coverage.md",
        "firmware/validate/coverage_fw.md",
        "firmware/validate/coverage_rtl.md",
        ],
        "description": "Collect FW and RTL coverage report steps and summaries.",
    },
    "Embedded Validation Report Agent": {
        "domain": "embedded",
        "inputs": ["spec_text", "toolchain", "toggles"],
        "outputs": ["firmware/validate/validation_report.md"],
        "description": "Generate validation report from cosim logs/coverage.",
    },

    # -------------------------
    # SYSTEM
    # -------------------------
    "System Workflow Agent": {
        "domain": "system",
        "inputs": ["*.v", "*.sv", "analog_netlist.cir", "main.cpp", "*_embedded_spec.json", "telemetry.json", "result_summary.txt"],
        "outputs": ["system_validation.json", "system_integration_notes.md"],
        "description": "Performs cross-loop/system-level sanity checks and produces integration notes across digital RTL, firmware, and analog artifacts.",
    },

    "System CoSim Integration Agent": {
        "domain": "system",
        "inputs": ["*_embedded_spec.json", "*.v", "*.sv"],
        "outputs": [
            "system/cosim/axi_lite_regs.sv",
            "system/cosim/ip_mmio.h",
            "system/cosim/cocotb_smoke_test.py",
            "system/cosim/README.md",
            "system_cosim_integration_agent.log",
        ],
        "description": "Generates co-sim scaffolding: AXI4-Lite register block template + firmware MMIO header + basic cocotb smoke test.",
    },

    "System ISS Bridge Agent": {
        "domain": "system",
        "inputs": ["*_embedded_spec.json", "*.v", "*.sv", "main.cpp"],
        "outputs": [
            "system/iss_bridge/README.md",
            "system/iss_bridge/iss_bridge_api.md",
            "system/iss_bridge/verilator_harness_skeleton.cpp",
            "system/iss_bridge/run_notes.md",
            "system_iss_bridge_agent.log",
        ],
        "description": "Generates scaffolding for an ISS<->RTL bridge (MMIO/IRQ contract + Verilator harness skeleton + run notes).",
    },

    "System Integration Intent Agent": {
        "domain": "system",
        "inputs": [
           "system_integration_description",
           "soc_integration_description",
           "integration_description",
           "digital_rtl_signatures",
           "rtl_signatures",
           "integrate/rtl_signatures.json",
           "analog_rtl_signatures",
           "analog_signatures",
           "analog_behavioral_module(optional)",
           "analog_macro_module(optional)",
           "top_module(optional)",
           "soc_top_name(optional)"
        ],
        "outputs": [
           "system/integration/system_integration_intent.json",
           "system/integration/system_integration_intent_raw.txt"
        ],
        "description": "Generates a strict SoC integration manifest for top assembly. Supports state-provided or artifact-discovered RTL signatures and sim/phys analog module overrides."
    },

 

    "System Top Assembly Agent": {
        "description": "Generates SoC top modules for simulation and physical design using integration manifest.",
        "inputs": [
            "system_integration_intent"
        ],
        "outputs": [
            "system/integration/soc_top_sim.sv",
            "system/integration/soc_top_phys.sv"
        ]
    },

    "System Testbench Generator Agent": {
        "domain": "system",
        "inputs": [
            "system/integration/system_integration_intent.json",
            "system/integration/soc_top_sim.sv",
            "system/integration/system_rtl_filelist_sim.txt",
            "digital/digital_regmap.json(optional)",
            "handoff/MANIFEST.json(optional)",
            "*.v",
            "*.sv"
        ],
        "outputs": [
            "vv/tb/test_*.py",
            "vv/tb/Makefile",
            "vv/tb/README.md",
            "vv/tb/tb_generation_report.json",
            "vv/testbench_generator_agent.log"
        ],
        "description": "Generates system-level Cocotb testbench collateral from system integration intent, assembled SoC top, system RTL filelist, and optional digital register-map/control metadata. Keeps stimulus generic and derived from system-level intent rather than protocol hardcoding.",
        "requires": ["cocotb", "verilator"],
    },

    "System Assertions (SVA) Agent": {
        "domain": "system",
        "inputs": [
            "system/integration/system_integration_intent.json",
            "system/integration/soc_top_sim.sv",
            "system/integration/system_rtl_filelist_sim.txt",
            "digital/digital_regmap.json(optional)",
            "*.v",
            "*.sv"
        ],
        "outputs": [
            "assertions.sv",
            "assertions_readme.md",
            "sva_agent.log"
        ],
        "description": "Generates top-level system assertions aligned to integration intent, ownership, reset behavior, and observable system behavior. Avoids protocol-specific hardcoding and focuses on system boundary correctness.",
        "requires": [],
    },

    "System Functional Coverage Agent": {
        "domain": "system",
        "inputs": [
            "system/integration/system_integration_intent.json",
            "system/integration/soc_top_sim.sv",
            "system/integration/system_rtl_filelist_sim.txt",
            "digital/digital_regmap.json(optional)",
            "*.v",
            "*.sv"
        ],
        "outputs": [
            "vv/tb/coverage_model.py",
            "vv/tb/COVERAGE.md",
            "vv/tb/coverage_generation_report.json",
            "functional_coverage_agent.log"
        ],
        "description": "Generates lightweight system-level functional coverage from integration intent, observable behaviors, ownership rules, and optional software-visible register/control metadata.",
        "requires": ["cocotb"],
    },

    "System Simulation Control Agent": {
        "domain": "system",
        "inputs": [
            "system/integration/system_integration_intent.json",
            "system/integration/soc_top_sim.sv",
            "system/integration/system_rtl_filelist_sim.txt",
            "vv/tb/Makefile",
            "vv/tb/test_*.py",
            "assertions.sv(optional)",
            "vv/tb/coverage_model.py(optional)",
            "*.v",
            "*.sv"
        ],
        "outputs": [
            "vv/tb/run_regression.py",
            "vv/tb/SIM_CONTROL.md",
            "vv/tb/simulation_manifest.json",
            "vv/tb/sim_control_generation_report.json",
            "simulation_control_agent.log"
        ],
        "description": "Generates system-level regression orchestration and simulation manifest for Cocotb/Verilator runs using assembled SoC top, system RTL filelist, generated testbench collateral, and optional assertions/coverage artifacts.",
        "requires": ["make", "cocotb", "verilator"],
    },

    "System Simulation Execution Agent": {
        "domain": "system",
        "inputs": [
            "system/integration/soc_top_sim.sv",
            "vv/tb/Makefile",
            "vv/tb/test_*.py",
            "vv/tb/coverage_model.py(optional)",
            "vv/tb/run_regression.py(optional)",
            "*.v",
            "*.sv"
        ],
        "outputs": [
            "system/sim/system_sim_execution.json",
            "system/sim/system_sim_execution.md",
            "system/sim/logs/*.log"
        ],
        "description": "Runs demo System_SIM execution for 2 testcases × 2 seeds using generated Cocotb/Verilator collateral; captures pass/fail, runtime, waveforms, and raw coverage candidates.",
        "requires": ["make", "verilator", "cocotb"],
    },

    "System Simulation Coverage Summary Agent": {
        "domain": "system",
        "inputs": [
            "system/sim/system_sim_execution.json",
            "vv/tb/coverage_model.py(optional)",
            "vv/tb/COVERAGE.md(optional)",
            "assertions.sv(optional)",
            "*.log(optional)"
        ],
        "outputs": [
            "system/sim/system_sim_dashboard.json",
            "system/sim/system_sim_dashboard.md"
        ],
        "description": "Parses System_SIM execution artifacts and publishes demo-friendly functional/code/assertion coverage plus run summary for UI display.",
        "requires": [],
    },


    "System Firmware CoSim Execution Agent": {
        "domain": "system",
        "inputs": [
            "system/integration/soc_top_sim.sv",
            "system/cosim/axi_lite_regs.sv(optional)",
            "system/cosim/ip_mmio.h(optional)",
            "firmware/validate/cocotb_harness.py(optional)",
            "firmware/validate/run_cosim.sh(optional)",
            "firmware/build/firmware.elf(optional)",
            "vv/tb/Makefile(optional)",
            "vv/tb/test_*.py(optional)",
            "vv/tb/coverage_model.py(optional)",
            "assertions.sv(optional)",
            "*.v",
            "*.sv"
        ],
        "outputs": [
            "system/firmware/cosim/system_firmware_execution.json",
            "system/firmware/cosim/system_firmware_execution.md",
            "system/firmware/cosim/system_firmware_dashboard.json",
            "system/firmware/cosim/logs/*.log",
            "system/firmware/cosim/waves/*.vcd"
        ],
        "description": "Runs firmware-aware RTL/SoC co-simulation using assembled SoC top, generated firmware collateral, cocotb/verilator harness, and optional assertions/coverage; captures pass/fail, runtime, logs, and waveforms.",
        "requires": ["make", "verilator", "cocotb"],
    },

    "System Firmware Coverage Summary Agent": {
        "domain": "system",
        "inputs": [
            "system/firmware/cosim/system_firmware_execution.json",
            "system/firmware/cosim/system_firmware_dashboard.json(optional)",
            "firmware/validate/coverage.md(optional)",
            "firmware/validate/coverage_fw.md(optional)",
            "firmware/validate/coverage_rtl.md(optional)",
            "validation/validation_report.md(optional)",
            "assertions.sv(optional)",
            "*.log(optional)"
        ],
        "outputs": [
            "system/firmware/coverage/system_firmware_coverage_summary.json",
            "system/firmware/coverage/system_firmware_coverage_summary.md"
        ],
        "description": "Parses firmware co-simulation execution artifacts and publishes demo-friendly firmware/RTL/assertion coverage summary plus overall run health for UI display.",
        "requires": [],
    },

    "System Implementation Setup Agent": {
        "domain": "system",
        "inputs": [
            "digital/foundry/foundry_profile.json",
            "system/integration/*_phys.sv",
            "system/integration/system_lib_filelist_phys.txt(optional)",
            "analog/abstract/macro.lef(optional)",
            "analog/abstract/macro_stub.lib(optional)",
            "*_spec.json(optional)",
            "*.sdc(optional)"
        ],
        "outputs": [
            "system/impl_setup/filelist.f",
            "system/impl_setup/macro_lefs.f",
            "system/impl_setup/macro_libs.f",
            "system/impl_setup/macro_gds.f",
            "system/impl_setup/constraints/*.sdc",
            "system/impl_setup/corners.json",
            "system/impl_setup/openlane/config.json",
            "system/impl_setup/logs/system_implementation_setup_input_resolution.log",
            "system/impl_setup/system_implementation_setup.log",
            "system/impl_setup/system_implementation_setup_summary.json"
        ],
        "description": (
            "Prepares System PD inputs for downstream Digital PD by selecting the physical SoC top "
            "and physical RTL filelist, resolving analog macro LEF/LIB/GDS collateral, generating "
            "canonical SDC + OpenLane config, and normalizing digital state handoff for synthesis."
        ),
        "requires": []
    },

    "System Software Handoff Package Agent": {
        "domain": "system",
        "inputs": [
            "firmware/firmware_manifest.json(optional)",
            "firmware/register_map.json(optional)",
            "system/integration/system_integration_intent.json(optional)",
            "system/integration/soc_top_sim.sv(optional)",
            "system/integration/system_rtl_filelist_sim.txt(optional)",
            "system/firmware/cosim/system_firmware_execution.json(optional)",
            "system/firmware/coverage/system_firmware_coverage_summary.json(optional)",
            "firmware/validate/validation_report.md(optional)",
            "firmware/validate/Makefile(optional)",
            "firmware/validate/test_*.py(optional)",
            "firmware/validate/cocotb_harness.py(optional)"
        ],
        "outputs": [
            "system/software_handoff/system_software_handoff.json",
            "system/software_handoff/system_software_handoff.md",
            "system/software_handoff/software_artifact_filelist.txt",
            "system/software_handoff/system_software_handoff_debug.json"
        ],
        "description": "Builds the machine-readable and human-readable software handoff package from System_Firmware outputs for downstream System_Software development.",
        "requires": [],
    },

    "System Software Handoff Ingest Agent": {
        "domain": "system",
        "inputs": [
            "system/software_handoff/system_software_handoff.json"
        ],
        "outputs": [
            "system/software/input/system_software_input_contract.json",
            "system/software/input/system_software_input_summary.md",
            "system/software/input/system_software_input_debug.json"
        ],
        "description": "Consumes the System_Firmware software handoff package, validates it, and produces a normalized input contract for downstream System_Software agents.",
        "requires": [],
    },

    "System Software Capability Model Agent": {
        "domain": "system",
        "inputs": [
            "system/software/input/system_software_input_contract.json"
        ],
        "outputs": [
            "system/software/model/system_software_capability_model.json",
            "system/software/model/system_software_capability_model.md"
        ],
        "description": "Builds a software-facing capability model from the validated handoff: platform services, register model summary, runtime constraints, and platform identity.",
        "requires": [],
    },

    "System Software API Contract Agent": {
        "domain": "system",
        "inputs": [
            "system/software/model/system_software_capability_model.json"
        ],
        "outputs": [
            "system/software/sdk/system_software_api_contract.json",
            "system/software/sdk/system_software_api_contract.md"
        ],
        "description": "Defines the public and internal System_Software API surface from the capability model, including service groups and method contracts.",
        "requires": [],
    },

    "System Software SDK Scaffold Agent": {
        "domain": "system",
        "inputs": [
            "system/software/sdk/system_software_api_contract.json"
        ],
        "outputs": [
            "system/software/sdk/system_software_sdk_manifest.json",
            "system/software/sdk/build_manifest.json",
            "system/software/sdk/README.md",
            "system/software/sdk/include/system_software_sdk.h",
            "system/software/sdk/src/lib.rs",
            "system/software/sdk/examples/example_app.c"
        ],
        "description": "Generates the initial System_Software SDK scaffold, including public header, Rust façade, example application, and build metadata.",
        "requires": [],
    },

     
    "System Software HAL/Driver Adapter Agent": {
        "domain": "system",
        "inputs": [
            "system/software/input/system_software_input_contract.json",
            "system/software/sdk/system_software_api_contract.json",
            "firmware/hal/registers.rs(optional)",
            "firmware/drivers/driver_scaffold.rs(optional)"
        ],
        "outputs": [
            "system/software/adapter/system_software_adapter_manifest.json",
            "system/software/adapter/system_software_adapter_summary.md",
            "system/software/adapter/system_software_adapter_debug.json",
            "system/software/adapter/src/hal_driver_adapter.rs"
        ],
        "description": "Builds a stable software adapter layer over firmware HAL, drivers, and register access for use by SDK and services.",
        "requires": [],
    },

    "System Software Config Schema Agent": {
        "domain": "system",
        "inputs": [
            "system/software/model/system_software_capability_model.json",
            "system/software/adapter/system_software_adapter_manifest.json(optional)"
        ],
        "outputs": [
            "system/software/config/system_software_config_schema.json",
            "system/software/config/system_software_config_summary.md",
            "system/software/config/system_software_config_debug.json",
            "system/software/config/default_config.json"
        ],
        "description": "Defines the runtime configuration schema, defaults, and software configuration contract for services and applications.",
        "requires": [],
    },

    "System Software Service Architecture Agent": {
        "domain": "system",
        "inputs": [
            "system/software/model/system_software_capability_model.json",
            "system/software/config/system_software_config_schema.json",
            "system/software/sdk/system_software_api_contract.json"
        ],
        "outputs": [
            "system/software/architecture/system_software_architecture.json",
            "system/software/architecture/system_software_architecture.md",
            "system/software/architecture/system_software_architecture_debug.json"
        ],
        "description": "Defines the System_Software architecture split across services, libraries, daemons, tools, and application-facing layers.",
        "requires": [],
    },

    "System Software Core Service Agent": {
        "domain": "system",
        "inputs": [
            "system/software/architecture/system_software_architecture.json",
            "system/software/config/system_software_config_schema.json",
            "system/software/adapter/system_software_adapter_manifest.json(optional)"
        ],
        "outputs": [
            "system/software/services/system_software_services_manifest.json",
            "system/software/services/system_software_services_summary.md",
            "system/software/services/system_software_services_debug.json",
            "system/software/services/src/*.rs"
        ],
        "description": "Generates core platform services such as init manager, health monitor, telemetry, diagnostics, and control service.",
        "requires": [],
    },

    "System Software Application Scaffold Agent": {
        "domain": "system",
        "inputs": [
            "system/software/sdk/system_software_sdk_manifest.json",
            "system/software/architecture/system_software_architecture.json",
            "system/software/services/system_software_services_manifest.json(optional)",
            "system/software/config/system_software_config_schema.json"
        ],
        "outputs": [
            "system/software/apps/system_software_apps_manifest.json",
            "system/software/apps/system_software_apps_summary.md",
            "system/software/apps/system_software_apps_debug.json",
            "system/software/apps/*"
        ],
        "description": "Generates sample applications and app templates using the SDK, APIs, configuration model, and core services.",
        "requires": [],
    },

        "System Software CLI / Tooling Agent": {
        "domain": "system",
        "inputs": [
            "system/software/apps/system_software_application_manifest.json",
            "system/software/sdk/system_software_sdk_manifest.json"
        ],
        "outputs": [
            "system/software/tools/system_software_tools_manifest.json",
            "system/software/tools/system_software_tools_summary.md",
            "system/software/tools/system_software_tools_debug.json",
            "system/software/tools/*/Cargo.toml",
            "system/software/tools/*/src/main.rs"
        ],
        "description": "Generates developer and operator CLI tools over the SDK.",
        "requires": [],
    },
    "System Software Build System Agent": {
        "domain": "system",
        "inputs": [
            "system/software/sdk/system_software_sdk_manifest.json",
            "system/software/apps/system_software_application_manifest.json",
            "system/software/tools/system_software_tools_manifest.json(optional)"
        ],
        "outputs": [
            "system/software/build/system_software_build_manifest.json",
            "system/software/build/system_software_build_summary.md",
            "system/software/build/system_software_build_debug.json",
            "system/software/build/Cargo.toml",
            "system/software/build/Makefile",
            "system/software/build/build_plan.json"
        ],
        "description": "Creates the workspace-level build plan and build orchestration files for System_Software.",
        "requires": [],
    },
    "System Software Unit Test Agent": {
        "domain": "system",
        "inputs": [
            "system/software/sdk/system_software_api_contract.json"
        ],
        "outputs": [
            "system/software/tests/system_software_test_manifest.json",
            "system/software/tests/system_software_test_summary.md",
            "system/software/tests/system_software_test_debug.json",
            "system/software/tests/sdk/sdk_tests.rs",
            "system/software/tests/services/service_tests.rs",
            "system/software/tests/apps/app_smoke.rs"
        ],
        "description": "Generates initial unit and smoke tests for the SDK, services, and applications.",
        "requires": [],
    },
    "System Software Mock Runtime Agent": {
        "domain": "system",
        "inputs": [
            "system/software/input/system_software_input_contract.json"
        ],
        "outputs": [
            "system/software/mock/system_software_mock_manifest.json",
            "system/software/mock/system_software_mock_summary.md",
            "system/software/mock/system_software_mock_debug.json",
            "system/software/mock/src/mock_runtime.rs"
        ],
        "description": "Builds a deterministic mock runtime for software-only bring-up and testing.",
        "requires": [],
    },
    "System Software Packaging Agent": {
        "domain": "system",
        "inputs": [
            "system/software/sdk/system_software_sdk_manifest.json",
            "system/software/apps/system_software_application_manifest.json",
            "system/software/tools/system_software_tools_manifest.json(optional)",
            "system/software/build/system_software_build_manifest.json(optional)",
            "system/software/tests/system_software_test_manifest.json(optional)",
            "system/software/mock/system_software_mock_manifest.json(optional)"
        ],
        "outputs": [
            "system/software/package/system_software_package.json",
            "system/software/package/system_software_package.md",
            "system/software/package/artifact_filelist.txt",
            "system/software/package/system_software_package_debug.json"
        ],
        "description": "Bundles the generated System_Software deliverables into a final package manifest and artifact list.",
        "requires": [],
    },

    "System Software Executive Summary Agent": {
        "domain": "system",
        "inputs": [
            "system/software/package/system_software_package.json",
            "system/software/sdk/system_software_sdk_manifest.json(optional)",
            "system/software/adapter/system_software_adapter_manifest.json(optional)",
            "system/software/services/system_software_core_services_manifest.json(optional)",
            "system/software/apps/system_software_application_manifest.json(optional)",
            "system/software/tools/system_software_tools_manifest.json(optional)",
            "system/software/build/system_software_build_manifest.json(optional)",
            "system/software/tests/system_software_test_manifest.json(optional)",
            "system/software/mock/system_software_mock_manifest.json(optional)",
            "system/software/model/system_software_capability_model.json(optional)",
            "system/software/input/system_software_input_contract.json(optional)"
        ],
        "outputs": [
            "system/software/executive/system_software_executive_summary.json",
            "system/software/executive/system_software_executive_summary.md",
            "system/software/executive/system_software_executive_summary_debug.json"
        ],
        "description": "Generates the final executive summary for System_Software by consolidating package readiness, delivery scope, technical coverage, risks, and next steps.",
        "requires": [],
    },
    # -------------------------
    # SYSTEM SOFTWARE VALIDATION (L1)
    # -------------------------

    "System Software Validation Ingest Agent": {
        "domain": "system",
        "inputs": ["system_software_workflow_id"],
        "outputs": ["system/software_validation/validation_manifest.json"],
        "description": "Restores system software artifacts from Supabase and prepares validation manifest.",
    },

    "System Software Build Validation Agent": {
        "domain": "system",
        "inputs": ["system/software_validation/validation_manifest.json"],
        "outputs": [
            "system/software_validation/build/build_validation_report.json",
            "system/software_validation/build/build_validation_summary.md"
        ],
        "description": "Runs cargo build and validates build success.",
    },

    "System Software Test Execution Agent": {
        "domain": "system",
        "inputs": ["system/software_validation/validation_manifest.json"],
        "outputs": [
            "system/software_validation/test/test_execution_report.json",
            "system/software_validation/test/test_execution_summary.md"
        ],
        "description": "Runs cargo test and validates test execution.",
    },

    "System Software Contract Consistency Agent": {
        "domain": "system",
        "inputs": ["system_software_api_contract.json", "system_software_sdk_manifest.json"],
        "outputs": ["system/software_validation/contract/contract_consistency_report.json"],
        "description": "Validates API ↔ SDK ↔ app contract consistency.",
    },

    "System Software Mock Runtime Validation Agent": {
        "domain": "system",
        "inputs": ["system/software_validation/validation_manifest.json"],
        "outputs": [
            "system/software_validation/mock/mock_runtime_validation_report.json",
            "system/software_validation/mock/mock_runtime_validation_summary.md"
        ],
        "description": "Validates mock runtime execution.",
    },

    "System Software Package Audit Agent": {
        "domain": "system",
        "inputs": ["system/software/package"],
        "outputs": [
            "system/software_validation/package/package_audit_report.json",
            "system/software_validation/package/package_audit_summary.md"
        ],
        "description": "Validates completeness of packaged artifacts.",
    },

    "System Software Validation Summary Agent": {
        "domain": "system",
        "inputs": ["system/software_validation/**"],
        "outputs": [
            "system/software_validation/summary/system_software_validation_summary.json",
            "system/software_validation/summary/system_software_validation_summary.md"
        ],
        "description": "Aggregates all validation signals into final readiness decision.",
    },  

    "System RTL Handoff Package Agent": {
        "domain": "system",
        "inputs": [
            "system_rtl_workflow_id",
            "system/integration/system_integration_intent.json(optional)",
            "system/integration/soc_top_sim.sv(optional)",
            "system/integration/soc_top_phys.sv(optional)",
            "system/integration/system_rtl_filelist_sim.txt(optional)",
            "system/integration/system_rtl_filelist_phys.txt(optional)",
            "system/integration/system_lib_filelist_phys.txt(optional)",
            "system/integration/system_full_compile_summary.json(optional)"
        ],
        "outputs": [
            "system/package/system_rtl_package.json",
            "system/package/system_rtl_package_summary.md",
            "system/package/system_rtl_package_debug.json"
        ],
        "description": (
            "Restores a prior System RTL workflow from Supabase/local artifacts and publishes a "
            "machine-readable System RTL handoff package containing top modules, sim/phys filelists, "
            "physical library filelist, compile evidence, readiness status, and restored collateral "
            "for downstream co-simulation and handoff flows."
        ),
        "requires": []
    },

    # -------------------------
    # Extended / optional digital flow agents
    # -------------------------
    "Power Intent Agent": {
        "domain": "digital",
        "inputs": ["structured_spec.json"],
        "outputs": ["power_intent.upf"],
        "description": "Generates UPF/CPF based on multi-power domain architecture.",
        "requires": ["multi_power_domain"],
    },

    "CDC Guard Agent": {
        "domain": "digital",
        "inputs": ["structured_spec.json"],
        "outputs": ["cdc_wrappers.v"],
        "description": "Inserts synchronizers based on CDC detection.",
        "requires": ["has_cdc"],
    },

    "PDC Guard Agent": {
        "domain": "digital",
        "inputs": ["structured_spec.json"],
        "outputs": ["pdc_wrappers.v"],
        "description": "Inserts level shifters and isolation.",
        "requires": ["has_pdc"],
    },

       # -------------------------
    # VALIDATION: Hardware Test Validation Loop (TestFlow-style)
    # -------------------------
    "Validation Instrument Setup Agent": {
        "domain": "validation",
        "inputs": ["user_id", "workflow_id", "instrument_ids(optional)"],
        "outputs": ["validation/bench_setup.json"],
        "description": "Resolves the user's bench setup from registered instruments (or defaults) and writes bench_setup.json for downstream validation agents.",
        "tags": ["validation", "bench", "instruments", "setup", "pyvisa", "scpi"],
    },

    "Validation Test Plan Agent": {
        "domain": "validation",
        "inputs": ["workflow_id", "datasheet_text", "goal(optional)"],
        "outputs": ["validation/test_plan.json"],
        "description": "Generates a structured hardware validation test plan from datasheet/spec text; uses generic instrument types (psu/dmm/scope).",
        "tags": ["validation", "test-plan", "datasheet", "llm", "coverage"],
    },

    "Validation Sequence Builder Agent": {
        "domain": "validation",
        "inputs": ["workflow_id", "bench_setup", "test_plan"],
        "outputs": ["validation/test_sequence.json"],
        "description": "Builds an executable SCPI test sequence (steps) from bench_setup + test_plan (initially Keysight-class examples; transport is PyVISA/SCPI).",
        "tags": ["validation", "sequence", "scpi", "pyvisa", "keysight", "automation"],
    },

    "Validation Execution Orchestrator Agent": {
        "domain": "validation",
        "inputs": ["workflow_id", "test_sequence"],
        "outputs": ["validation/results.json", "validation/results.csv", "validation/run_manifest.json"],
        "description": "Executes the validation test_sequence and produces results artifacts. MVP uses a stub executor; next step swaps in real PyVISA I/O.",
        "tags": ["validation", "execution", "orchestrator", "results", "pyvisa", "scpi"],
    },

    "Validation Analytics Agent": {
        "domain": "validation",
        "inputs": ["workflow_id", "test_plan", "validation_results"],
        "outputs": [
            "validation/analytics.json",
            "validation/analytics.md",
            "validation/results_evaluated.json",
        ],
        "description": "Applies test_plan measurement limits (min/max) to captured results and generates analytics + a demo-ready report.",
        "tags": ["validation", "analytics", "limits", "pass-fail", "report"],
    },

    "Validation Scope Agent": {
        "domain": "validation",
        "inputs": ["workflow_id", "test_plan", "scope(optional)"],
        "outputs": ["validation/scope_selection.json", "validation/scoped_test_plan.json"],
        "description": "Applies user-selected scope (by tags or test names) to the generated validation test_plan and produces a scoped test plan for downstream sequence building.",
        "tags": ["validation", "scope", "selection", "filter", "test-plan"],
    },

    "Validation Connectivity Intent Agent": {
        "domain": "validation",
        "inputs": ["workflow_id", "test_plan OR scoped_test_plan"],
        "outputs": ["validation/connectivity_intent.json"],
        "description": "Phase-1: Generates logical connectivity intent (bench template) from test plan. No physical resource strings; reusable across labs.",
        "tags": ["validation", "phase-1", "handoff", "connectivity", "bench-template", "wiring"],
    },

    "Validation Wiring Instructions Agent": {
        "domain": "validation",
        "inputs": ["workflow_id", "connectivity_intent"],
        "outputs": ["validation/wiring_instructions.md"],
        "description": "Phase-1: Generates human-readable lab wiring instructions from connectivity intent (lab never logs into ChipLoop).",
        "tags": ["validation", "phase-1", "handoff", "wiring", "instructions", "lab"],
    },

    "Validation Preflight Agent": {
        "domain": "validation",
        "inputs": ["workflow_id", "user_id", "bench_setup", "test_plan OR scoped_test_plan (recommended)"],
        "outputs": ["validation/preflight_report.json", "validation/preflight_summary.md"],
        "description": "Phase-2a: Safe bench readiness checks (coverage + resource string sanity + optional *IDN?); no DUT stimulus. Supports stub or pyvisa mode.",
        "tags": ["validation", "preflight", "bench", "readiness", "scpi", "pyvisa", "stub"],
    },

    "Validation Bench Create Agent": {
        "domain": "validation",
        "inputs": ["workflow_id", "user_id", "bench_name", "instrument_ids"],
        "outputs": ["validation/bench_create_report.json", "validation/bench_create_summary.md"],
        "description": "Creates a new validation bench and maps selected instruments to it. Outputs a creation report and summary.",
        "tags": ["validation", "bench", "create", "instruments", "mapping"],
    },

    "Validation Test Plan Load Agent": {
        "domain": "validation",
        "inputs": ["workflow_id", "user_id", "test_plan_id"],
        "outputs": ["validation/test_plan_loaded.json", "validation/test_plan_loaded_summary.md"],
        "description": "Loads a previously saved validation test plan from the database using test_plan_id and makes it available as state['test_plan'] for execution workflows (no datasheet/spec needed).",
        "tags": ["validation", "test-plan", "load", "db", "reuse", "execution"],
    },

    "Validation Bench Schematic Agent": {
        "domain": "validation",
        "inputs": ["workflow_id", "user_id", "bench_id"],
        "outputs": ["validation/bench_schematic.json", "validation/bench_schematic_summary.md"],
        "description": "Generates bench_schematic.json (instruments + basic rail/probe templates) and persists it to validation_bench_connections.schematic for preflight/run mapping.",
        "tags": ["validation", "bench", "schematic", "wiring", "mapping"],
    },

    "Validation Bench Schematic Load + Mapping Agent": {
        "domain": "validation",
        "inputs": ["workflow_id", "user_id", "bench_id", "bench_setup"],
        "outputs": [
           "validation/bench_schematic_loaded.json",
           "validation/execution_mapping.json",
           "validation/execution_mapping_summary.md",
        ],
        "description": "Loads bench schematic from validation_bench_connections and reconciles it with runtime bench_setup to generate execution_mapping.json for WF4.",
        "tags": ["validation", "bench", "schematic", "mapping", "execution"],
    },

    "Validation Pattern Detection Agent": {
        "domain": "validation",
        "inputs": ["workflow_id", "bench_id", "test_plan_id", "pattern_window_days(optional)"],
        "outputs": [
            "validation/patterns.json",
            "validation/patterns_summary.md",
        ],
        "description": "Analyzes historical validation runs (facts + interpretations) for a given bench_id + test_plan_id and detects recurring clusters, flaky tests, and correlations. Writes patterns artifacts only; does not mutate WF4 execution artifacts.",
        "tags": ["validation", "cognition", "patterns", "flaky", "correlation", "insights"],
    },

    "Validation Test Evolution Agent": {
        "domain": "validation",
        "inputs": ["workflow_id", "user_id(optional)", "bench_id", "test_plan_id"],
        "outputs": [
            "validation/evolution_status.md",
            "validation/proposed_test_plan.json",
            "validation/evolution_diff.md",
            "validation/evolution_rationale.md",
        ],
        "description": "Cognition (Phase 3): Proposes a versioned evolution of the existing test plan based on actionable failures/flakiness for the SAME bench_id + test_plan_id. HARD no-op if no actionable failure evidence. Proposal only; no auto-mutation.",
        "tags": ["validation", "cognition", "evolution", "proposal", "no-op-if-pass", "guardrails"],
    },

    "Validation Apply Proposal Agent": {
        "domain": "validation",
        "inputs": ["workflow_id", "user_id", "base_test_plan_id(or test_plan_id)", "proposal_artifact_path(or proposed_test_plan_json)", "proposal_kind(optional)"],
        "outputs": ["validation/apply_status.md"],
        "description": "Applies a proposed test plan (from evolution or coverage proposal) by inserting vNext into validation_test_plans, deactivating previous active plan(s) for that user+name, and activating the new version. Deterministic; no LLM.",
        "tags": ["validation", "cognition", "apply", "versioning", "activation", "loop-closure"],
    },

    "Validation Evolution Proposal Agent": {
        "domain": "validation",
        "inputs": ["workflow_id", "user_id", "bench_id", "test_plan_name"],
        "outputs": [
          "validation/evolution_status.md",
          "validation/evolution_no_action.md",
          "validation/proposed_test_plan.json",
          "validation/evolution_plan_diff.md",
          "validation/evolution_rationale.md",
        ],
        "description": "WF7: Failure-driven diagnostic proposal. Hard no-op if no actionable failures found.",
        "tags": ["validation", "cognition", "evolution", "proposal"],
    },

    "Validation Coverage Proposal Agent": {
        "domain": "validation",
        "inputs": ["workflow_id", "user_id", "bench_id", "test_plan_name"],
        "outputs": [
          "validation/coverage_map.json",
          "validation/coverage_gaps.json",
          "validation/coverage_summary.md",
          "validation/coverage_no_action.md",
          "validation/proposed_test_plan_from_coverage.json",
          "validation/coverage_plan_diff.md",
          "validation/coverage_plan_rationale.md",
        ],
        "description": "WF8: Coverage intelligence + proposal. Computes gaps from recent run facts and proposes coverage tests.",
        "tags": ["validation", "cognition", "coverage", "proposal"],
    },


    
}



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
        "outputs": ["clock_reset_architecture.json", "clock_reset_architecture_agent.log"],
        "description": "Defines clock/reset intent: clock domains, reset strategies, and CDC-aware intent (no implementation).",
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
        "inputs": ["*_spec.json", "*.v", "*.sv", "clock_reset_architecture.json"],
        "outputs": ["cdc_report.md", "cdc_findings.json", "cdc_analysis_agent.log"],
        "description": "Analyzes clock-domain crossings and flags required synchronizers/handshakes (intent-level, not tool-specific implementation).",
    },

    "Digital Reset Integrity Agent": {
        "domain": "digital",
        "inputs": ["*_spec.json", "*.v", "*.sv", "clock_reset_architecture.json"],
        "outputs": ["reset_integrity_report.md", "reset_integrity_findings.json", "reset_integrity_agent.log"],
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
        "inputs": ["*_spec.json", "*.v", "*.sv", "digital_architecture.json", "digital_microarchitecture.json", "regmap.json", "clock_reset_architecture.json"],
        # Generated/uploaded by the agent into vv/tb (filenames are stable except test_<top>.py)
        "outputs": [
            "vv/tb/test_*.py",
            "vv/tb/Makefile",
            "vv/tb/README.md",
            "vv/tb/tb_generation_report.json",
            "testbench_generator_agent.log",
        ],
        "description": "Generates Cocotb testbench skeletons (directed + constrained-random stub) and a Verilator-friendly Makefile using spec-derived clocks/resets/ports.",
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

    # -------------------------
    # ANALOG
    # -------------------------
    # -------------------------
    # ANALOG (NEW, FRESH)
    # -------------------------
    "Analog Spec Builder Agent": {
        "domain": "analog",
        "inputs": ["datasheet_text", "goal", "scope"],
        "outputs": ["analog/spec.json", "analog/spec_summary.md"],
        "description": "Extracts a structured analog block spec (ports, modes, targets, corners, validation intent).",
    },
    "Analog Netlist Scaffold Agent": {
        "domain": "analog",
        "inputs": ["analog/spec.json"],
        "outputs": ["analog/netlist.sp", "analog/netlist_summary.md"],
        "description": "Generates a SPICE netlist scaffold aligned to spec pins and block intent.",
    },
    "Analog Simulation Plan Agent": {
        "domain": "analog",
        "inputs": ["analog/spec.json", "analog/netlist.sp"],
        "outputs": ["analog/sim_plan.json", "analog/run_deck.sp"],
        "description": "Creates sweeps/corners/metrics plan and an example run deck template.",
    },
    "Analog Behavioral Model Agent": {
        "domain": "analog",
        "inputs": ["analog/spec.json"],
        "outputs": ["analog/model.sv", "analog/model.va", "analog/model_params.json", "analog/model_notes.md"],
        "description": "Creates RNM SystemVerilog or Verilog-A behavioral model template + tuning params.",
    },
    "Analog Behavioral Testbench Agent": {
        "domain": "analog",
        "inputs": ["analog/spec.json", "analog/model.sv"],
        "outputs": ["analog/tb.sv"],
        "description": "Generates SystemVerilog testbench stimuli for the behavioral model.",
    },
    "Analog Behavioral Assertions Agent": {
        "domain": "analog",
        "inputs": ["analog/spec.json", "analog/model.sv"],
        "outputs": ["analog/sva.sv"],
        "description": "Generates SV assertions/checkers for enable sequencing, limits, settling windows, etc.",
    },
    "Analog Behavioral Coverage Agent": {
        "domain": "analog",
        "inputs": ["analog/spec.json"],
        "outputs": ["analog/coverage_plan.json", "analog/validation_summary.md"],
        "description": "Defines scenario/corner/sweep coverage intent for analog validation.",
    },
    "Analog Correlation Agent": {
        "domain": "analog",
        "inputs": ["analog/spec.json", "analog/sim_plan.json", "analog/model.sv"],
        "outputs": ["analog/metrics_compare.json", "analog/delta_summary.json", "analog/correlation_report.md"],
        "description": "Defines metric correlation between behavioral and netlist (stimulus-matched correlation).",
    },
    "Analog Iteration Proposal Agent": {
        "domain": "analog",
        "inputs": ["analog/delta_summary.json", "analog/model.sv"],
        "outputs": ["analog/iteration_patch.diff", "analog/iteration_rationale.md", "analog/next_run_plan.json"],
        "description": "Proposes tuning/code patches and re-run plan based on correlation deltas.",
    },
    "Analog Abstract Views Agent": {
        "domain": "analog",
        "inputs": ["analog/spec.json"],
        "outputs": ["analog/abstracts/macro.lef", "analog/abstracts/macro_stub.lib", "analog/abstracts/integration_notes.md"],
        "description": "Generates LEF + LIB stub + integration notes for physical/timing handoff.",
    },
    "Analog Executive Summary Agent": {
        "domain": "analog",
        "inputs": ["*"],
        "outputs": ["analog/executive_summary.md"],
        "description": "Creates an executive-style summary for the analog workflow/app.",
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
        "inputs": ["spec_text", "toolchain", "toggles"],
        "outputs": ["firmware/register_map.json"],
        "description": "Extract registers/CSRs from spec/regmap sources and produce a normalized register map.",
    },
    "Embedded Rust Register Layer Generator Agent": {
        "domain": "embedded",
        "inputs": ["spec_text", "toolchain", "toggles"],
        "outputs": ["firmware/hal/registers.rs"],
        "description": "Generate Rust HAL register abstractions from register map.",
    },
    "Embedded Register Validation Agent": {
        "domain": "embedded",
        "inputs": ["spec_text", "toolchain", "toggles"],
        "outputs": ["firmware/hal/register_validation.md"],
        "description": "Validate register map consistency and HAL correctness.",
    },
    "Embedded Rust Driver Scaffold Agent": {
        "domain": "embedded",
        "inputs": ["spec_text", "toolchain", "toggles"],
        "outputs": ["firmware/drivers/driver_scaffold.rs"],
        "description": "Generate driver scaffold, init, basic read/write APIs.",
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
    
    "Embedded ELF Build Agent": {
        "domain": "embedded",
        "inputs": ["spec_text", "toolchain", "toggles"],
        "outputs": [
        "firmware/build/build_instructions.md",
        "firmware/build/Cargo.toml",
        "firmware/build/.cargo/config.toml",
        "firmware/build/memory.x",
        "firmware/src/main.rs",
        "firmware/src/panic.rs",
        ],
        "description": "Generate Cargo build instructions and ELF build steps.",
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



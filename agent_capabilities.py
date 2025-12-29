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
    # ANALOG
    # -------------------------
    "Analog Spec Agent": {
        "domain": "analog",
        "inputs": [],
        "outputs": ["analog_spec.json"],
        "description": "Creates an analog block spec JSON (interfaces, performance targets, constraints).",
    },

    "Analog Netlist Agent": {
        "domain": "analog",
        "inputs": ["analog_spec.json"],
        "outputs": ["analog_netlist.cir", "analog_netlist_agent.log"],
        "description": "Generates a baseline SPICE netlist scaffold from an analog spec.",
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
}



# backend/agent_capabilities.py

AGENT_CAPABILITIES = {
    "Digital Spec Agent": {
        "domain": "digital",
        "inputs": [],
        # Digital spec agent typically produces a structured *_spec.json and one or more RTL modules
        "outputs": ["*_spec.json", "*.v"],
        "description": "Creates a structured digital spec JSON and baseline Verilog modules from user input."
    },
    "Digital RTL Agent": {
        "domain": "digital",
        # RTL agent consumes the generated spec JSON + RTL files for validation / lint / compile
        "inputs": ["*_spec.json", "*.v"],
        "outputs": ["rtl_agent_compile.log", "rtl_agent_summary.txt"],
        "description": "Validates and compiles generated Verilog RTL against the spec; produces logs and summary."
    },
    "Analog Spec Agent": {
        "domain": "analog",
        "inputs": [],
        "outputs": ["analog_spec.json"],
        "description": "Captures analog design intent as JSON spec."
    },
    "Analog Netlist Agent": {
        "domain": "analog",
        "inputs": ["analog_spec.json"],
        "outputs": ["analog_netlist.cir"],
        "description": "Converts analog spec into SPICE netlist."
    },
    "Embedded Spec Agent": {
        "domain": "embedded",
        # If digital spec exists, embedded spec can optionally use it, but it can also run standalone
        "inputs": ["*_spec.json"],
        "outputs": ["*_embedded_spec.json"],
        "description": "Creates firmware spec JSON describing how software interacts with the digital block."
    },
    "Embedded Code Agent": {
        "domain": "embedded",
        "inputs": ["*_embedded_spec.json"],
        "outputs": ["main.c"],
        "description": "Generates embedded C firmware from the embedded spec."
    },

    # ✅ NEW: include simulation + demo result agents so the planner can select them
    "Embedded Sim Agent": {
        "domain": "embedded",
        "inputs": ["main.c", "*_embedded_spec.json", "*.v"],
        "outputs": ["telemetry.json", "simulation.log"],
        "description": "Simulates firmware behavior over time and generates structured telemetry for demos/analysis."
    },
    "Embedded Result Agent": {
        "domain": "embedded",
        "inputs": ["telemetry.json"],
        "outputs": ["result_summary.txt"],
        "description": "Generates a short demo-ready summary report based on telemetry."
    },

    "System Workflow Agent": {
        "domain": "system",
        # System flow can orchestrate or validate across loops; include sim/result outputs as possible dependencies
        "inputs": ["*.v", "analog_netlist.cir", "main.c", "telemetry.json", "result_summary.txt"],
        "outputs": ["system_validation.json"],
        "description": "Performs cross-loop integration and validation; can include demo artifacts (telemetry/results)."
    },
}

# Preserve your extended agents section (unchanged except for keeping the file consistent)
AGENT_CAPABILITIES.update({
    "Power Intent Agent": {
        "domain": "digital",
        "inputs": ["structured_spec.json"],
        "outputs": ["power_intent.upf"],
        "description": "Generates UPF/CPF based on multi-power domain architecture.",
        "requires": ["multi_power_domain"]
    },
    "CDC Guard Agent": {
        "domain": "digital",
        "inputs": ["structured_spec.json"],
        "outputs": ["cdc_wrappers.v"],
        "description": "Inserts synchronizers based on CDC detection.",
        "requires": ["has_cdc"]
    },
    "PDC Guard Agent": {
        "domain": "digital",
        "inputs": ["structured_spec.json"],
        "outputs": ["pdc_wrappers.v"],
        "description": "Inserts level shifters and isolation.",
        "requires": ["has_pdc"]
    }
})

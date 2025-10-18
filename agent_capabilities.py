# backend/agent_capabilities.py
AGENT_CAPABILITIES = {
    "Digital Spec Agent": {
        "domain": "digital",
        "inputs": [],
        "outputs": ["digital_spec.json"],
        "description": "Creates structured digital spec JSON from user input."
    },
    "Digital RTL Agent": {
        "domain": "digital",
        "inputs": ["digital_spec.json"],
        "outputs": ["rtl.v"],
        "description": "Generates Verilog RTL from digital spec."
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
        "inputs": [],
        "outputs": ["embedded_spec.json"],
        "description": "Creates firmware spec JSON."
    },
    "Embedded Code Agent": {
        "domain": "embedded",
        "inputs": ["embedded_spec.json"],
        "outputs": ["main.c"],
        "description": "Generates embedded C firmware."
    },
    "System Workflow Agent": {
        "domain": "system",
        "inputs": ["rtl.v", "analog_netlist.cir", "main.c"],
        "outputs": ["system_validation.json"],
        "description": "Performs cross-loop integration and validation."
    },
}

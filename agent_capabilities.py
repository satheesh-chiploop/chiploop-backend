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

# backend/agent_capabilities.py
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

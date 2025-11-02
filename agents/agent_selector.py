from agent_capabilities import AGENT_CAPABILITIES

def select_required_agents(structured_spec_final):
    agents = []

    # --- Feature flags ---
    has_multi_power = len(structured_spec_final.get("power_domains", [])) > 1
    has_cdc = len(structured_spec_final.get("cdc_crossings", [])) > 0
    has_pdc = len(structured_spec_final.get("pdc_crossings", [])) > 0

    if has_multi_power:
        agents.append("Power Intent Agent")

    if has_cdc:
        agents.append("CDC Guard Agent")

    if has_pdc:
        agents.append("PDC Guard Agent")

    # --- One compute agent per module ---
    mod_name = (structured_spec_final.get("module", {}) or {}).get("name")
    if mod_name:
        agents.append(f"{mod_name}_compute_agent")

    # --- De-duplicate while preserving order ---
    seen = set()
    return [a for a in agents if not (a in seen or seen.add(a))]

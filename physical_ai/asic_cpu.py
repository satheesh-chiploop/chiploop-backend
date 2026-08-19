from typing import Any, Dict

from .toolchain_targets import resolve_rust_toolchain


def resolve_asic_cpu_config(raw: Any, *, deployment_architecture: str, policy: Any = None) -> Dict[str, Any]:
    enabled = deployment_architecture == "asic_soc"
    if not enabled:
        return {"schema": "chiploop.asic.cpu_ip.v1", "enabled": False}
    if not isinstance(policy, dict) or not isinstance(policy.get("asic_soc_cpu"), dict):
        raise ValueError("Supabase processor_ip_policy.asic_soc_cpu is required")
    section = policy["asic_soc_cpu"]
    defaults = section.get("defaults") if isinstance(section.get("defaults"), dict) else {}
    requested = {**defaults, **(dict(raw) if isinstance(raw, dict) else {})}
    core_request = str(requested.get("core") or "automatic").lower()
    core = str(section.get("default_core") or "") if core_request == "automatic" else core_request
    catalog = section.get("cores") if isinstance(section.get("cores"), dict) else {}
    if not core or core not in catalog:
        raise ValueError(f"unsupported Supabase-governed ASIC CPU IP core: {core_request}")
    spec = catalog[core]
    isa_request = str(requested.get("isa") or "automatic").lower()
    isa = str(spec.get("default_isa") or "") if isa_request == "automatic" else isa_request
    if isa not in list(spec.get("supported_isas") or []):
        raise ValueError(f"{spec.get('label') or core} does not support requested ISA {isa}")
    bus_request = str(requested.get("bus") or "automatic").lower()
    bus = str(spec.get("default_bus") or "") if bus_request == "automatic" else bus_request
    if bus not in set(section.get("allowed_buses") or []):
        raise ValueError(f"unsupported ASIC CPU bus: {bus}")
    clock_mhz, boot_rom_kib, sram_kib = float(requested["clock_mhz"]), int(requested["boot_rom_kib"]), int(requested["sram_kib"])
    if clock_mhz <= 0 or boot_rom_kib < 4 or sram_kib < 8:
        raise ValueError("ASIC CPU clock must be positive, boot ROM at least 4 KiB, and SRAM at least 8 KiB")
    gate = section.get("integration_gate") if isinstance(section.get("integration_gate"), dict) else {}
    toolchain = resolve_rust_toolchain(spec, isa, default_abi="ilp32")
    return {"schema": "chiploop.asic.cpu_ip.v1", "policy_schema": policy.get("schema"), "enabled": True, "selection_mode": "automatic" if core_request == "automatic" else "advanced_override", "core": core, "core_label": spec.get("label"), "license": spec.get("license"), "profile": spec.get("profile"), "isa": isa, "abi": toolchain["target_abi"], **toolchain, "bus": bus, "clock_mhz": clock_mhz, "boot_rom_kib": boot_rom_kib, "sram_kib": sram_kib, "interrupts": bool(requested.get("interrupts")), "debug": bool(requested.get("debug")), "clock_gating": bool(requested.get("clock_gating")), "dft_scan_required": bool(requested.get("dft_scan_required")), "integration_gate": {"cpu_rtl_required": bool(gate.get("cpu_rtl_required")), "memory_macro_mapping_required": bool(gate.get("memory_macro_mapping_required")), "complete_soc_synthesis_required": bool(gate.get("complete_soc_synthesis_required")), "status": str(gate.get("default_status") or "")}}

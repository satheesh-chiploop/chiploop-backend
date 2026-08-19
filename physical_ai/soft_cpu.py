from typing import Any, Dict

from .toolchain_targets import resolve_rust_toolchain


def resolve_soft_cpu_config(raw: Any, *, deployment_architecture: str, policy: Any = None) -> Dict[str, Any]:
    enabled = deployment_architecture == "fpga_soft_cpu"
    if not enabled:
        return {"schema": "chiploop.fpga.soft_cpu.v1", "enabled": False}
    if not isinstance(policy, dict) or not isinstance(policy.get("fpga_soft_cpu"), dict):
        raise ValueError("Supabase processor_ip_policy.fpga_soft_cpu is required")
    section = policy["fpga_soft_cpu"]
    defaults = section.get("defaults") if isinstance(section.get("defaults"), dict) else {}
    requested = {**defaults, **(dict(raw) if isinstance(raw, dict) else {})}
    core_request = str(requested.get("core") or "automatic").lower()
    core = str(section.get("default_core") or "") if core_request == "automatic" else core_request
    catalog = section.get("cores") if isinstance(section.get("cores"), dict) else {}
    if not core or core not in catalog:
        raise ValueError(f"unsupported Supabase-governed soft CPU core: {core_request}")
    spec = catalog[core]
    isa_request = str(requested.get("isa") or "automatic").lower()
    isa = str(spec.get("default_isa") or "") if isa_request == "automatic" else isa_request
    if isa not in list(spec.get("supported_isas") or []):
        raise ValueError(f"{spec.get('label') or core} does not support requested ISA {isa}")
    bus_request = str(requested.get("bus") or "automatic").lower()
    bus = str(spec.get("default_bus") or "") if bus_request == "automatic" else bus_request
    if bus not in set(section["allowed_buses"]):
        raise ValueError(f"unsupported soft CPU bus: {bus}")
    instruction_kib, data_kib = int(requested["instruction_memory_kib"]), int(requested["data_memory_kib"])
    clock_mhz = float(requested["clock_mhz"])
    if min(instruction_kib, data_kib) < 4 or clock_mhz <= 0:
        raise ValueError("soft CPU memories must be at least 4 KiB and clock must be positive")
    toolchain = resolve_rust_toolchain(spec, isa, default_abi="ilp32")
    return {"schema": "chiploop.fpga.soft_cpu.v1", "policy_schema": policy.get("schema"), "enabled": True, "selection_mode": "automatic" if core_request == "automatic" else "advanced_override", "core": core, "core_label": spec.get("label"), "license": spec.get("license"), "profile": spec.get("profile"), "isa": isa, "abi": toolchain["target_abi"], **toolchain, "compiler_arch": isa, "bus": bus, "clock_mhz": clock_mhz, "instruction_memory_kib": instruction_kib, "data_memory_kib": data_kib, "interrupts": bool(requested.get("interrupts")), "uart": bool(requested.get("uart")), "debug": bool(requested.get("debug")), "estimated_reservation": {"logic_cells": int(spec["estimated_logic_cells"]), "block_ram_blocks": int(spec["estimated_bram_blocks"]), "basis": "supabase_governed_reference_estimate", "must_be_replaced_by_complete_system_synthesis": True}}

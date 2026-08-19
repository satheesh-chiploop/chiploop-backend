from typing import Any, Dict, Iterable

from .toolchain_targets import resolve_rust_toolchain

POLICY_SCHEMA = "chiploop.application_intelligence.processor_ip_policy.v2"
DEPLOYMENT_MODES = {"automatic", "fpga_onboard_cpu", "fpga_soft_cpu", "fpga_external_host", "asic_digital_ip", "asic_soc", "asic_companion"}


def _require(mapping: Dict[str, Any], keys: Iterable[str], path: str) -> None:
    missing = [key for key in keys if key not in mapping or mapping[key] in (None, "", [])]
    if missing:
        raise ValueError(f"Supabase {path} is missing required fields: {', '.join(missing)}")


def validate_processor_policy(policy: Any) -> Dict[str, Any]:
    if not isinstance(policy, dict):
        raise ValueError("Supabase processor_ip_policy is required")
    if policy.get("schema") != POLICY_SCHEMA:
        raise ValueError(f"Supabase processor_ip_policy.schema must be {POLICY_SCHEMA}")
    _require(policy, ["automatic_fpga_deployment", "automatic_asic_deployment", "fpga_hard_cpu", "fpga_soft_cpu", "asic_soc_cpu"], "processor_ip_policy")
    for default_key in ("automatic_fpga_deployment", "automatic_asic_deployment"):
        if str(policy[default_key]) not in DEPLOYMENT_MODES:
            raise ValueError(f"Supabase processor_ip_policy.{default_key} is invalid")
    for section_name, memory_keys in (("fpga_soft_cpu", ("clock_mhz", "instruction_memory_kib", "data_memory_kib")), ("asic_soc_cpu", ("clock_mhz", "boot_rom_kib", "sram_kib"))):
        section = policy[section_name]
        if not isinstance(section, dict):
            raise ValueError(f"Supabase processor_ip_policy.{section_name} must be an object")
        _require(section, ["availability", "default_core", "defaults", "allowed_buses", "cores", "integration_gate"], f"processor_ip_policy.{section_name}")
        _require(section["defaults"], memory_keys, f"processor_ip_policy.{section_name}.defaults")
        if not isinstance(section["cores"], dict) or not section["cores"] or section["default_core"] not in section["cores"]:
            raise ValueError(f"Supabase processor_ip_policy.{section_name} core catalog/default is invalid")
        for core, spec in section["cores"].items():
            _require(spec, ["label", "license", "default_isa", "supported_isas", "default_bus"], f"processor_ip_policy.{section_name}.cores.{core}")
            if str(section.get("availability")) == "production" or bool(spec.get("integration_ready")):
                _require(
                    spec,
                    ["source_url", "source_revision", "source_sha256", "rtl_top"],
                    f"processor_ip_policy.{section_name}.cores.{core}",
                )
            if spec["default_bus"] not in section["allowed_buses"]:
                raise ValueError(f"Supabase core {core} default_bus is not allowed")
            for isa in spec.get("supported_isas") or []:
                try:
                    resolve_rust_toolchain(spec, str(isa), default_abi=str(spec.get("target_abi") or ""))
                except ValueError as exc:
                    raise ValueError(
                        f"Supabase processor_ip_policy.{section_name}.cores.{core} has no valid "
                        f"Rust toolchain contract for ISA {isa}: {exc}"
                    ) from exc
        gate = section["integration_gate"]
        _require(gate, ["cpu_rtl_required", "complete_system_synthesis_required", "default_status"], f"processor_ip_policy.{section_name}.integration_gate")
        if gate["default_status"] not in {"ready", "pending_cpu_rtl", "unavailable"}:
            raise ValueError(f"Supabase {section_name} integration gate status is invalid")
    return policy


def integration_is_ready(section: Dict[str, Any], core: Dict[str, Any]) -> bool:
    gate = section["integration_gate"]
    return str(section["availability"]) == "production" and str(gate["default_status"]) == "ready" and bool(core.get("integration_ready"))

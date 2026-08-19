import re
from typing import Any, Dict


def canonical_rust_target_triple(target_triple: str, target_isa: str = "") -> str:
    """Normalize architecture-contract RISC-V names to Rust target names."""
    target = str(target_triple or "").strip()
    isa = str(target_isa or "").strip().lower().replace("-", "").replace("_", "")

    bare_match = re.fullmatch(r"rv(32|64)([a-z0-9]+)", target.lower())
    if bare_match:
        return f"riscv{bare_match.group(1)}{bare_match.group(2)}-unknown-none-elf"

    triple_match = re.fullmatch(r"rv(32|64)([a-z0-9]+)(-.+)", target.lower())
    if triple_match:
        return f"riscv{triple_match.group(1)}{triple_match.group(2)}{triple_match.group(3)}"

    if target.lower() in {"riscv32-unknown-none-elf", "riscv64-unknown-none-elf"}:
        isa_match = re.fullmatch(r"rv(32|64)([a-z0-9]+)", isa)
        if isa_match:
            return f"riscv{isa_match.group(1)}{isa_match.group(2)}-unknown-none-elf"
    return target


def resolve_rust_toolchain(core_spec: Dict[str, Any], isa: str, *, default_abi: str = "") -> Dict[str, Any]:
    """Resolve a policy-declared Rust toolchain without assuming a CPU family."""
    isa_key = str(isa or "").strip().lower()
    per_isa = core_spec.get("toolchains") or core_spec.get("rust_targets") or {}
    selected = per_isa.get(isa_key) if isinstance(per_isa, dict) else None
    if isinstance(selected, str):
        selected = {"target_triple": selected}
    selected = selected if isinstance(selected, dict) else {}

    configured_target = (
        selected.get("target_triple")
        or selected.get("rust_target")
        or selected.get("custom_target_json")
        or core_spec.get("target_triple")
        or core_spec.get("rust_target")
        or core_spec.get("custom_target_json")
        or isa_key
    )
    target_triple = canonical_rust_target_triple(configured_target, isa_key)
    # Only RISC-V ISA notation has a safe legacy derivation. Every other CPU
    # family must declare the compiler target explicitly in its policy entry.
    if target_triple == isa_key and not re.fullmatch(r"riscv(32|64)[a-z0-9]+-unknown-.+", target_triple):
        raise ValueError(
            f"CPU policy ISA '{isa_key}' must declare toolchains.{isa_key}.target_triple "
            "(or a custom_target_json); target inference is only supported for RISC-V ISA notation."
        )

    abi = str(
        selected.get("abi")
        or selected.get("target_abi")
        or core_spec.get("abi")
        or core_spec.get("target_abi")
        or default_abi
    ).strip()
    features = selected.get("features") or selected.get("compiler_features") or core_spec.get("compiler_features") or []
    if isinstance(features, str):
        features = [item.strip() for item in features.split(",") if item.strip()]
    return {
        "target_triple": target_triple,
        "target_abi": abi,
        "compiler_features": list(features) if isinstance(features, list) else [],
        "custom_target_json": selected.get("custom_target_json") or core_spec.get("custom_target_json"),
    }

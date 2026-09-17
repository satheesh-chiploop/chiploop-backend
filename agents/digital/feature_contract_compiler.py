import hashlib
import re
from typing import Any, Dict, Iterable, List


REQUIREMENT_KEYS = (
    "features", "requirements", "functional_requirements", "verification_requirements", "behavior_rules",
)


def _items(spec: Dict[str, Any]) -> Iterable[Any]:
    owners = [spec]
    hierarchy = spec.get("hierarchy") if isinstance(spec.get("hierarchy"), dict) else {}
    top = hierarchy.get("top_module") if isinstance(hierarchy.get("top_module"), dict) else {}
    owners.append(top)
    for owner in owners:
        for key in REQUIREMENT_KEYS:
            value = owner.get(key)
            if isinstance(value, list):
                yield from value
            elif value not in (None, "", {}):
                yield value


def _mapping(item: Dict[str, Any], keys: tuple[str, ...]) -> Dict[str, Any]:
    for key in keys:
        value = item.get(key)
        if isinstance(value, dict):
            return dict(value)
    return {}


def _integer(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _integer(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_integer(item) for item in value]
    if isinstance(value, (bool, int, float)):
        return int(value) if isinstance(value, bool) else value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "high", "asserted", "on"}:
            return 1
        if text in {"false", "low", "deasserted", "off"}:
            return 0
        try:
            return int(text, 0)
        except ValueError:
            return value
    return value


def _resolve_map(values: Dict[str, Any], names: Dict[str, str]) -> tuple[Dict[str, Any], List[str]]:
    resolved: Dict[str, Any] = {}
    unresolved: List[str] = []
    for raw_name, value in values.items():
        canonical = names.get(str(raw_name).strip().lower())
        if canonical:
            resolved[canonical] = _integer(value)
        else:
            unresolved.append(str(raw_name))
    return resolved, unresolved


def compile_feature_contracts(
    spec: Dict[str, Any], ports: List[Dict[str, Any]], registers: Dict[str, int] | None = None
) -> List[Dict[str, Any]]:
    """Compile explicit feature semantics without inventing missing expected behavior."""
    port_names = {
        str(port.get("name") or "").lower(): str(port.get("name"))
        for port in ports if port.get("name")
    }
    register_names = {str(name).lower(): str(name) for name in (registers or {})}
    contracts: List[Dict[str, Any]] = []
    seen = set()
    for index, raw in enumerate(_items(spec)):
        item = raw if isinstance(raw, dict) else {"statement": str(raw)}
        statement = str(
            item.get("statement") or item.get("description") or item.get("requirement")
            or item.get("name") or raw
        ).strip()
        if not statement:
            continue
        explicit_id = item.get("feature_id") or item.get("requirement_id") or item.get("id") or item.get("name")
        feature_id = re.sub(r"[^a-z0-9]+", "_", str(explicit_id or "").lower()).strip("_")
        if not feature_id:
            feature_id = f"feature_{index + 1}_{hashlib.sha256(statement.encode()).hexdigest()[:8]}"
        if feature_id in seen:
            continue
        seen.add(feature_id)

        stimulus_raw = _mapping(item, ("stimulus", "inputs", "given", "preconditions"))
        expected_raw = _mapping(item, ("expected", "expected_behavior", "outputs", "then"))
        stimulus, unresolved_stimulus = _resolve_map(stimulus_raw, port_names)
        expected, unresolved_expected = _resolve_map(expected_raw, port_names)
        mentioned = [name for key, name in port_names.items() if re.search(rf"\b{re.escape(key)}\b", statement.lower())]
        mentioned_registers = [name for key, name in register_names.items() if re.search(rf"\b{re.escape(key)}\b", statement.lower())]
        wait_cycles = _integer(item.get("wait_cycles") or item.get("deadline_cycles") or item.get("within_cycles") or 1)
        if not isinstance(wait_cycles, (int, float)):
            wait_cycles = 1
        executable = bool(expected) and not unresolved_stimulus and not unresolved_expected
        contracts.append({
            "feature_id": feature_id,
            "statement": statement,
            "stimulus": stimulus,
            "expected": expected,
            "wait_cycles": max(1, int(wait_cycles)),
            "monitors": list(dict.fromkeys([*expected.keys(), *mentioned])),
            "registers": mentioned_registers,
            "coverage_bins": [f"{feature_id}.stimulus_applied", f"{feature_id}.expected_observed"],
            "executable": executable,
            "status": "executable" if executable else "trace_only",
            "non_executable_reason": None if executable else (
                "Requirement lacks an explicit, fully bound expected signal map."
                if not expected else
                "Requirement references stimulus/expected names that are not declared top-level signals."
            ),
            "unresolved_bindings": sorted(set(unresolved_stimulus + unresolved_expected)),
        })
    return contracts

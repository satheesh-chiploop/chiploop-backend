from typing import Any, Dict


QUALIFIED_EXTERNAL_HOST_PROTOCOLS = {"spi"}
REQUIRED_EXTERNAL_HOST_INTERFACE_FIELDS = {
    "protocol", "role", "clock_mhz", "data_width_bits", "framing",
    "flow_control", "interrupt_signaling", "register_access",
}


def validate_external_host_interface_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(plan) if isinstance(plan, dict) else {}
    missing = sorted(
        key for key in REQUIRED_EXTERNAL_HOST_INTERFACE_FIELDS
        if normalized.get(key) in {None, ""}
    )
    if missing:
        raise ValueError(
            "External-host FPGA mode requires an interface plan before RTL generation; missing: "
            + ", ".join(missing)
        )
    protocol = str(normalized.get("protocol") or "").strip().lower()
    if protocol not in QUALIFIED_EXTERNAL_HOST_PROTOCOLS:
        raise ValueError(
            f"External-host protocol {protocol!r} is not yet qualified. Qualified protocols: "
            + ", ".join(sorted(QUALIFIED_EXTERNAL_HOST_PROTOCOLS))
            + "."
        )
    try:
        clock_mhz = float(normalized["clock_mhz"])
        data_width_bits = int(normalized["data_width_bits"])
    except (TypeError, ValueError) as exc:
        raise ValueError("External-host clock_mhz and data_width_bits must be numeric.") from exc
    if not 0 < clock_mhz <= 100:
        raise ValueError("Qualified SPI clock_mhz must be greater than 0 and no more than 100 MHz.")
    required_spi_values = {
        "role": "fpga_peripheral",
        "data_width_bits": 8,
        "framing": "register_command_response",
        "flow_control": "chip_select_and_status",
        "register_access": "addressed_read_write",
    }
    mismatched = [key for key, expected in required_spi_values.items() if normalized.get(key) != expected]
    if mismatched:
        raise ValueError(
            "The qualified SPI adapter does not implement the requested values for: "
            + ", ".join(sorted(mismatched))
        )
    if str(normalized.get("interrupt_signaling")) not in {"optional_gpio", "status_polling"}:
        raise ValueError("Qualified SPI interrupt_signaling must be optional_gpio or status_polling.")
    normalized["protocol"] = protocol
    normalized["clock_mhz"] = clock_mhz
    normalized["data_width_bits"] = data_width_bits
    return normalized

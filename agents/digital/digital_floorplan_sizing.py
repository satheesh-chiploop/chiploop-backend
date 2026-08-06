import math
import os
import re


def _range_width(value: str | None) -> int:
    match = re.fullmatch(r"\[\s*(-?\d+)\s*:\s*(-?\d+)\s*\]", str(value or "").strip())
    if not match:
        return 1
    return abs(int(match.group(1)) - int(match.group(2))) + 1


def top_level_io_bits(rtl_files: list[str], top_module: str) -> int:
    """Count scalar-equivalent top-level ports for deterministic die sizing."""
    top = re.escape(str(top_module or "").strip())
    if not top:
        return 0
    for path in rtl_files or []:
        try:
            text = open(path, "r", encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        text = re.sub(r"/\*.*?\*/|//[^\r\n]*", "", text, flags=re.DOTALL)
        module = re.search(rf"\bmodule\s+{top}\b(?:\s*#\s*\(.*?\))?\s*\((.*?)\)\s*;", text, flags=re.DOTALL)
        if not module:
            continue
        header = module.group(1)
        total = 0
        # Works for the ANSI form produced by Arch2RTL. Non-ANSI declarations
        # are counted from the module body below.
        ansi = list(re.finditer(
            r"\b(?:input|output|inout)\b\s*(?:(?:wire|logic|reg)\s*)?(?:signed\s*)?(\[[^\]]+\])?\s*([^,\)]+)",
            header,
        ))
        if ansi:
            for declaration in ansi:
                names = [item.strip() for item in declaration.group(2).split(",") if item.strip()]
                total += _range_width(declaration.group(1)) * max(1, len(names))
            return total
        end = re.search(r"\bendmodule\b", text[module.end():])
        body = text[module.end():module.end() + (end.start() if end else len(text))]
        for declaration in re.finditer(
            r"\b(?:input|output|inout)\b\s*(?:(?:wire|logic|reg)\s*)?(?:signed\s*)?(\[[^\]]+\])?\s*([^;]+);",
            body,
        ):
            names = [item.strip() for item in declaration.group(2).split(",") if item.strip()]
            total += _range_width(declaration.group(1)) * len(names)
        return total
    return 0


def implementation_die_area(rtl_files: list[str], top_module: str) -> tuple[str, int, float]:
    """Return a conservative square die large enough for the top-level pins."""
    io_bits = top_level_io_bits(rtl_files, top_module)
    extra_bands = max(0, math.ceil((io_bits - 48) / 40.0))
    side_um = float(max(120, 120 + (40 * extra_bands)))
    return f"0 0 {side_um:g} {side_um:g}", io_bits, side_um

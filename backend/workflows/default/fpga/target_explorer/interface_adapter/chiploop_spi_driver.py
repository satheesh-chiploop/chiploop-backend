"""Generated ChipLoop SPI Mode-0 host transport.

Command N is committed when chip-select rises.  The response visible during
that exchange is the previous core snapshot, so callers should pipeline or
perform a second exchange when they need the response to command N.
"""
from typing import Callable, Mapping

INPUT_BITS = 4
OUTPUT_BITS = 4
FRAME_BITS = 4
FRAME_BYTES = (FRAME_BITS + 7) // 8
INPUT_MAP = [{'port': 'debug_addr', 'width': 4, 'lsb': 0}]
OUTPUT_MAP = [{'port': 'led', 'width': 4, 'lsb': 0}]

def _mask(width: int) -> int:
    return (1 << width) - 1

def pack_command(values: Mapping[str, int]) -> bytes:
    frame = 0
    for field in INPUT_MAP:
        value = int(values.get(field["port"], 0))
        if value < 0 or value > _mask(field["width"]):
            raise ValueError(f'{field["port"]} exceeds {field["width"]} bits')
        frame |= value << field["lsb"]
    return frame.to_bytes(FRAME_BYTES, "big")

def unpack_response(raw: bytes) -> dict[str, int]:
    if len(raw) != FRAME_BYTES:
        raise ValueError(f"expected {FRAME_BYTES} response bytes, got {len(raw)}")
    # Response bits leave the FPGA first and are followed by zero padding when
    # the command side determines a longer full-duplex frame.
    value = int.from_bytes(raw, "big") >> (FRAME_BYTES * 8 - OUTPUT_BITS)
    return {field["port"]: (value >> field["lsb"]) & _mask(field["width"]) for field in OUTPUT_MAP}

class ChipLoopSpiDevice:
    def __init__(self, transfer: Callable[[bytes], bytes]):
        self._transfer = transfer

    def exchange(self, values: Mapping[str, int]) -> dict[str, int]:
        response = self._transfer(pack_command(values))
        return unpack_response(response)

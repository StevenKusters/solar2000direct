"""Reading address-packed blocks from a device, and finding out which ones it accepts.

Wide reads are the whole performance argument, but they are also all-or-nothing: a span
covering one register the firmware does not implement fails entirely, taking every
register in it down. So spans are validated against the actual device once at startup,
narrowing wherever it objects, and the narrowed plan is what the collector then uses.

Shared by the benchmark and the collector so both agree on what "this block works" means.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from huawei_solar import SUN2000Device
from huawei_solar.exceptions import ConnectionInterruptedException
from huawei_solar.registers import REGISTERS

_LOGGER = logging.getLogger(__name__)

BlockResult = tuple[dict[str, Any], list[list[str]], list[str]]


def block_span(block: list[str]) -> tuple[int, int]:
    """Start address and register count covered by a block."""
    start = REGISTERS[block[0]].register
    last = REGISTERS[block[-1]]
    return start, last.register + last.length - start


async def read_block_adaptive(device: SUN2000Device, block: list[str], depth: int = 0) -> BlockResult:
    """Read a block, halving it and retrying if the device rejects the span.

    Returns the values read, the sub-blocks that worked, and the names that could not be
    read at all. A dropped connection is re-raised rather than bisected -- splitting the
    span will not help if the socket is gone, and the caller needs to reconnect.
    """
    try:
        values = await device.client.get_multiple_as_dict(block)
    except ConnectionInterruptedException:
        raise
    except Exception as err:  # noqa: BLE001 - an unreadable span is a result to record
        if len(block) == 1:
            _LOGGER.info("Register %s is unreadable on this device (%s)", block[0], type(err).__name__)
            return {}, [], list(block)
        midpoint = len(block) // 2
        start, span = block_span(block)
        _LOGGER.info("Span %d+%d rejected (%s), splitting", start, span, type(err).__name__)
        left = await read_block_adaptive(device, block[:midpoint], depth + 1)
        right = await read_block_adaptive(device, block[midpoint:], depth + 1)
        return (
            {**left[0], **right[0]},
            [*left[1], *right[1]],
            [*left[2], *right[2]],
        )
    else:
        return values, [block], []


async def validate_plan(
    device: SUN2000Device,
    plan: list[list[str]],
    on_block: Callable[[list[str], list[list[str]]], None] | None = None,
) -> tuple[list[list[str]], list[str]]:
    """Confirm the device tolerates every planned span, narrowing where it does not.

    ``on_block`` is called with the original block and the sub-blocks that worked, so
    callers can report progress without this function knowing how they want to print it.
    """
    working: list[list[str]] = []
    unreadable: list[str] = []
    for block in plan:
        _values, blocks, bad = await read_block_adaptive(device, block)
        if on_block is not None:
            on_block(block, blocks)
        working.extend(blocks)
        unreadable.extend(bad)
    return working, unreadable

"""Read named registers from the inverter, for when a specific number is in question.

Deliberately separate from the collector: this opens its own short-lived connection, so
it competes for the inverter's single Modbus slot. Stop the collector before using it
against the same device, or expect both to see interruptions.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fnmatch
import sys

from huawei_solar import create_device_instance, create_tcp_client
from huawei_solar.registers import REGISTERS

from solar2000direct.registers import build_read_plan


async def run(args: argparse.Namespace) -> int:
    matched: list[str] = []
    for pattern in args.patterns:
        if pattern in REGISTERS:
            matched.append(pattern)
            continue
        found = sorted(name for name in REGISTERS if fnmatch.fnmatch(name, pattern))
        if not found:
            print(f"No register matches {pattern!r}", file=sys.stderr)
        matched.extend(found)

    names = list(dict.fromkeys(matched))
    if not names:
        return 1

    if args.list_only:
        for name in names:
            print(f"{REGISTERS[name].register:<7} {name}")
        return 0

    client = create_tcp_client(args.host, args.port, unit_id=args.unit_id, timeout=args.timeout)
    await client.connect()
    try:
        device = await create_device_instance(client)
        for block in build_read_plan(names):
            try:
                values = await device.client.get_multiple_as_dict(block)
            except Exception as err:  # noqa: BLE001 - report and continue to the next block
                print(f"  ! block starting at {REGISTERS[block[0]].register} failed: {err}", file=sys.stderr)
                continue
            for name, result in values.items():
                unit = f" {result.unit}" if result.unit else ""
                print(f"{name:<44} {result.value!r}{unit}")
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="s2d-read",
        description="Read named registers. Patterns may use shell wildcards.",
        epilog="example: s2d-read 192.168.1.50 'cumulative_dc_energy_yield_mppt*' daily_yield_energy",
    )
    parser.add_argument("host")
    parser.add_argument("patterns", nargs="+", help="Register names or glob patterns")
    parser.add_argument("--port", type=int, default=502)
    parser.add_argument("--unit-id", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=10)
    parser.add_argument("--list-only", action="store_true", help="Show matching names and addresses, read nothing")
    args = parser.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

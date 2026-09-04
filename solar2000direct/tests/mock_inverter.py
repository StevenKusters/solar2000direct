"""A fake SUN2000 speaking just enough Modbus TCP to exercise the probe and collector.

Real hardware is a bad development dependency: it accepts one client at a time, it is
slow on purpose, and pointing test code at a live installation risks knocking Home
Assistant off the bus. This server implements function code 0x03 over a word store
seeded with plausible values, and deliberately reproduces the two behaviours that
matter for correctness:

* it answers only for its configured unit ID, staying silent for every other one, so
  unit-ID scanning is exercised against real timeouts rather than tidy errors;
* it adds configurable per-request latency, so timing and cadence logic sees numbers
  in the same range a real SDongle produces.

Usage::

    python tests/mock_inverter.py --port 5502 --unit-id 1 --battery-units 2 --meter
    python -m solar2000direct.probe 127.0.0.1 --port 5502 --unit-id 1
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import struct
from collections import defaultdict

READ_HOLDING_REGISTERS = 0x03
WRITE_SINGLE_REGISTER = 0x06
WRITE_MULTIPLE_REGISTERS = 0x10
HUAWEI_SUBFUNCTION = 0x41
LOGIN_CHALLENGE = 0x24
LOGIN = 0x25
ILLEGAL_FUNCTION = 0x01
ILLEGAL_DATA_ADDRESS = 0x02
MAX_REGISTERS_PER_READ = 125

# Fixed rather than random so a run is reproducible; the real inverter uses fresh entropy.
MOCK_CHALLENGE = bytes(range(0x10, 0x20))


def _digest(password: bytes, seed: bytes) -> bytes:
    """The inverter's challenge-response construction: HMAC-SHA256 keyed on the password
    hash. Implemented here so the write path can be exercised end to end without touching
    a grid-connected inverter."""
    return hmac.digest(key=hashlib.sha256(password).digest(), msg=seed, digest=hashlib.sha256)


class RegisterStore:
    """A sparse 16-bit word store addressed by Huawei's raw register numbers."""

    def __init__(self) -> None:
        self._words: dict[int, int] = defaultdict(int)

    def read(self, address: int, count: int) -> list[int]:
        return [self._words[address + offset] & 0xFFFF for offset in range(count)]

    def set_words(self, address: int, words: list[int]) -> None:
        for offset, word in enumerate(words):
            self._words[address + offset] = word & 0xFFFF

    def set_u16(self, address: int, value: int) -> None:
        self.set_words(address, [value])

    def set_i16(self, address: int, value: int) -> None:
        self.set_words(address, list(struct.unpack(">H", struct.pack(">h", value))))

    def set_u32(self, address: int, value: int) -> None:
        self.set_words(address, list(struct.unpack(">HH", struct.pack(">I", value))))

    def set_i32(self, address: int, value: int) -> None:
        self.set_words(address, list(struct.unpack(">HH", struct.pack(">i", value))))

    def set_string(self, address: int, text: str, length_words: int) -> None:
        raw = text.encode("ascii")[: length_words * 2].ljust(length_words * 2, b"\x00")
        self.set_words(address, list(struct.unpack(f">{length_words}H", raw)))


def build_store(*, battery_units: int, meter: bool, optimizers: int) -> RegisterStore:
    """Seed a store that looks like a plausible three-phase residential installation."""
    store = RegisterStore()

    # Identity block
    store.set_string(30000, "SUN2000-8KTL-M1", 15)  # model_name
    store.set_string(30015, "HV2140012345", 10)  # serial_number
    store.set_string(30025, "02320MQK", 10)  # pn
    store.set_string(30035, "V100R001C00SPC141", 15)  # firmware_version
    store.set_string(30050, "V200R022C10SPC121", 15)  # software_version
    store.set_u16(30071, 2)  # nb_pv_strings -- east and west
    store.set_u32(30073, 8000)  # rated_power (W)
    store.set_u16(37200, optimizers)  # nb_optimizers

    # A physically coherent mid-afternoon scenario, because incoherent test data hides
    # real bugs. PV 4126 W in; 1200 W to the battery; 2850 W out of the inverter; 1650 W
    # exported; leaving 1200 W of house load. house = inverter + grid = 2850 - 1650.
    store.set_i16(32016, 4123)  # pv_01_voltage, gain 10 -> 412.3 V
    store.set_i16(32017, 612)  # pv_01_current, gain 100 -> 6.12 A  (2523 W, east)
    store.set_i16(32018, 3980)  # pv_02_voltage -> 398.0 V
    store.set_i16(32019, 402)  # pv_02_current -> 4.02 A  (1600 W, west)
    store.set_i32(32064, 4126)  # input_power (W)

    # AC side. Phase currents are deliberately near-identical: a three-phase SUN2000
    # injects symmetrically, and the per-phase load derivation depends on detecting that.
    store.set_u16(32066, 4177)  # line_voltage_A_B, gain 10
    store.set_u16(32067, 4199)
    store.set_u16(32068, 4161)
    store.set_u16(32069, 2409)  # phase_A_voltage -> 240.9 V
    store.set_u16(32070, 2435)  # phase_B_voltage -> 243.5 V
    store.set_u16(32071, 2392)  # phase_C_voltage -> 239.2 V
    store.set_i32(32072, 3944)  # phase_A_current, gain 1000 -> 3.944 A
    store.set_i32(32074, 3902)  # phase_B_current -> 3.902 A
    store.set_i32(32076, 3972)  # phase_C_current -> 3.972 A
    store.set_i32(32080, 2850)  # active_power (W)
    store.set_u16(32086, 9812)  # efficiency, gain 100 -> 98.12 %
    store.set_i16(32087, 412)  # internal_temperature, gain 10 -> 41.2 C
    store.set_u16(32088, 2500)  # insulation_resistance, gain 1000 -> 2.5 MOhm
    store.set_u16(32089, 512)  # device_status
    store.set_u32(32106, 2_492_882)  # accumulated_yield_energy, gain 100 -> 24928.82 kWh
    store.set_u32(32114, 3_352)  # daily_yield_energy, gain 100 -> 33.52 kWh
    store.set_u32(32212, 1_284_500)  # cumulative_dc_energy_yield_mppt1, gain 100
    store.set_u32(32214, 1_208_300)  # cumulative_dc_energy_yield_mppt2, gain 100

    if battery_units >= 1:
        store.set_u16(47000, 2)  # storage_unit_1_product_model = HUAWEI_LUNA2000
        store.set_u16(47954, 0)  # storage_capacity_control_mode (readable => supported)
        store.set_u16(37004, 74)  # storage_unit_1_state_of_capacity, gain 10 -> 7.4 %
        store.set_u16(37760, 682)  # storage_state_of_capacity -> 68.2 %
        store.set_i32(37765, -2400)  # storage_charge_discharge_power (W), negative = discharging
        store.set_u16(37004, 682)  # storage_unit_1_state_of_capacity -> 68.2 %
        store.set_i32(37001, 600)  # storage_unit_1_charge_discharge_power (charging)
        store.set_i16(37022, 214)  # storage_unit_1_battery_temperature -> 21.4 C
        store.set_u16(37760, 676)  # storage_state_of_capacity -> 67.6 %
        store.set_i32(37765, 1200)  # storage_charge_discharge_power, total charging
        store.set_u32(37780, 894_300)  # storage_total_charge, gain 100 -> 8943.00 kWh
        store.set_u32(37782, 812_600)  # storage_total_discharge
        _seed_battery_packs(store, unit=1, base_soc=68.2)
        # Battery configuration, as a summer (maximise self-consumption) setup looks.
        store.set_u32(37758, 30000)  # storage_rated_capacity, Wh
        store.set_u16(47081, 1000)  # charging cutoff 100.0 %, gain 10
        store.set_u16(47082, 0)  # discharging cutoff 0.0 %
        store.set_u16(47086, 2)  # working mode = MAXIMISE_SELF_CONSUMPTION
        store.set_u16(47087, 1)  # charge from grid enabled
        store.set_u16(47088, 900)  # grid charge cutoff 90.0 %
        store.set_u16(47102, 150)  # backup power SOC 15.0 %
        store.set_u32(47075, 5000)  # maximum charging power
        store.set_u32(47077, 5000)  # maximum discharging power
        store.set_u32(47244, 5000)  # maximum power of charge from grid, W
        store.set_u16(47299, 1)  # excess PV in TOU = CHARGE
        store.set_u16(47954, 0)  # capacity control disabled
        store.set_u16(47955, 0)
    if battery_units >= 2:
        store.set_u16(47089, 2)  # storage_unit_2_product_model
        store.set_u16(37738, 671)  # storage_unit_2_state_of_capacity -> 67.1 %
        store.set_i32(37743, 600)  # storage_unit_2_charge_discharge_power (charging)
        _seed_battery_packs(store, unit=2, base_soc=67.1)

    if meter:
        store.set_u16(37100, 1)  # meter_status = NORMAL
        store.set_u16(37125, 1)  # meter_type = three phase
        store.set_i32(37113, -1650)  # power_meter_active_power (W), negative = exporting
        store.set_i16(37118, 4997)  # active_grid_frequency, gain 100 -> 49.97 Hz
        store.set_i32(37121, 2_217_131)  # grid_accumulated_energy (imported), gain 100
        store.set_i32(37119, 779_707)  # grid_exported_energy, gain 100
        # Symmetric injection of 950 W per phase against asymmetric household loads of
        # 50 / 450 / 700 W. The meter therefore sees -900 / -500 / -250, summing to -1650.
        store.set_i32(37132, -900)  # active_grid_A_power
        store.set_i32(37134, -500)  # active_grid_B_power
        store.set_i32(37136, -250)  # active_grid_C_power

    return store


# Base addresses per storage unit, from the Huawei register map. Packs are 42 registers
# apart within a unit, and the six temperature pairs sit together in their own block.
_PACK_BASE = {1: 38229, 2: 38355}
_PACK_TEMP_BASE = {1: 38452, 2: 38458}
_PACK_STRIDE = 42


def _seed_battery_packs(store: RegisterStore, unit: int, base_soc: float) -> None:
    """Give each of a unit's three packs slightly different values.

    Identical packs would let a bug that reads the same pack three times pass unnoticed,
    which is exactly the bug that would break the spread calculation.
    """
    base = _PACK_BASE[unit]
    for index, pack in enumerate((1, 2, 3)):
        offset = base + index * _PACK_STRIDE
        soc = base_soc - index * 0.4
        store.set_u16(offset, round(soc * 10))  # state_of_capacity, gain 10
        store.set_i32(offset + 4, 200 - index * 6)  # charge_discharge_power, W (charging)
        store.set_u16(offset + 6, round((51.2 + index * 0.15) * 10))  # voltage, gain 10
        store.set_i16(offset + 7, round((3.9 - index * 0.1) * 10))  # current, gain 10

        temp_base = _PACK_TEMP_BASE[unit] + index * 2
        store.set_i16(temp_base, round((22.8 + index * 0.6) * 10))  # maximum temperature
        store.set_i16(temp_base + 1, round((20.1 + index * 0.5) * 10))  # minimum temperature


class MockInverter:
    def __init__(self, store: RegisterStore, unit_id: int, latency: float, password: str = "") -> None:
        self.store = store
        self.unit_id = unit_id
        self.latency = latency
        self.password = password
        self.request_count = 0
        self.logged_in = False
        self.writes: list[tuple[int, list[int]]] = []

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        print(f"  [mock] connection from {peer}")
        try:
            while True:
                header = await reader.readexactly(7)
                transaction_id, _protocol_id, length, unit_id = struct.unpack(">HHHB", header)
                pdu = await reader.readexactly(length - 1)

                # Real devices simply do not answer for unit IDs they do not serve.
                if unit_id != self.unit_id:
                    continue

                if self.latency:
                    await asyncio.sleep(self.latency)

                response = self._build_response(pdu)
                self.request_count += 1
                writer.write(
                    struct.pack(">HHHB", transaction_id, 0, len(response) + 1, unit_id) + response,
                )
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            print(f"  [mock] {peer} disconnected after {self.request_count} requests")
            writer.close()

    def _build_response(self, pdu: bytes) -> bytes:
        function_code = pdu[0]
        if function_code == READ_HOLDING_REGISTERS:
            address, count = struct.unpack(">HH", pdu[1:5])
            if not 1 <= count <= MAX_REGISTERS_PER_READ:
                return bytes([function_code | 0x80, ILLEGAL_DATA_ADDRESS])
            words = self.store.read(address, count)
            return bytes([function_code, count * 2]) + struct.pack(f">{count}H", *words)

        if function_code == WRITE_SINGLE_REGISTER:
            address, value = struct.unpack(">HH", pdu[1:5])
            if not self._may_write():
                return bytes([function_code | 0x80, 0x80])  # permission denied
            self.store.set_words(address, [value])
            self.writes.append((address, [value]))
            return pdu[:5]

        if function_code == WRITE_MULTIPLE_REGISTERS:
            address, count, byte_count = struct.unpack(">HHB", pdu[1:6])
            if not self._may_write():
                return bytes([function_code | 0x80, 0x80])
            words = list(struct.unpack(f">{count}H", pdu[6 : 6 + byte_count]))
            self.store.set_words(address, words)
            self.writes.append((address, words))
            print(f"  [mock] wrote {count} register(s) at {address}: {words}")
            return struct.pack(">BHH", function_code, address, count)

        if function_code == HUAWEI_SUBFUNCTION:
            return self._build_subfunction_response(pdu)

        return bytes([function_code | 0x80, ILLEGAL_FUNCTION])

    def _may_write(self) -> bool:
        """Writes require an installer session, exactly as the real device does."""
        if not self.password:
            return True
        if not self.logged_in:
            print("  [mock] rejected a write: not logged in")
        return self.logged_in

    def _build_subfunction_response(self, pdu: bytes) -> bytes:
        sub = pdu[1]
        if sub == LOGIN_CHALLENGE:
            # Content length is 17: sixteen challenge bytes plus one trailing byte.
            return bytes([HUAWEI_SUBFUNCTION, LOGIN_CHALLENGE, 17, *MOCK_CHALLENGE, 0x00])

        if sub == LOGIN:
            client_challenge = pdu[3:19]
            username_length = pdu[19]
            offset = 20 + username_length
            username = pdu[20:offset].decode("utf-8", "replace")
            digest_length = pdu[offset]
            supplied = pdu[offset + 1 : offset + 1 + digest_length]

            expected = _digest(self.password.encode(), MOCK_CHALLENGE)
            self.logged_in = hmac.compare_digest(supplied, expected)
            print(f"  [mock] login as {username!r}: {'accepted' if self.logged_in else 'REJECTED'}")
            if not self.logged_in:
                return bytes([HUAWEI_SUBFUNCTION, LOGIN, 2, 1, 0])

            mac = _digest(self.password.encode(), client_challenge)
            return bytes([HUAWEI_SUBFUNCTION, LOGIN, 2 + len(mac), 0, len(mac), *mac])

        return bytes([HUAWEI_SUBFUNCTION | 0x80, ILLEGAL_FUNCTION])


async def serve(args: argparse.Namespace) -> None:
    store = build_store(battery_units=args.battery_units, meter=args.meter, optimizers=args.optimizers)
    inverter = MockInverter(store, args.unit_id, args.latency_ms / 1000, args.password)
    server = await asyncio.start_server(inverter.handle, args.host, args.port)
    print(
        f"  [mock] SUN2000-8KTL-M1 on {args.host}:{args.port} unit {args.unit_id} "
        f"(battery units={args.battery_units}, meter={args.meter}, latency={args.latency_ms}ms)",
    )
    async with server:
        await server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Fake SUN2000 for local development.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5502)
    parser.add_argument("--unit-id", type=int, default=1)
    parser.add_argument("--battery-units", type=int, default=0, choices=(0, 1, 2))
    parser.add_argument("--meter", action="store_true")
    parser.add_argument("--optimizers", type=int, default=0)
    parser.add_argument("--latency-ms", type=float, default=40.0, help="Simulated per-request delay")
    parser.add_argument("--password", default="", help="Installer password required before writes are accepted")
    args = parser.parse_args()
    try:
        asyncio.run(serve(args))
    except KeyboardInterrupt:
        print("\n  [mock] stopped")


if __name__ == "__main__":
    main()

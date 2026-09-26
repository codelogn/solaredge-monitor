"""
Reads the inverter directly over Modbus TCP (SunSpec), which is the only
way to see the AC side at all — the cloud API exposes optimizer DC data
only. That blind spot mattered: ruling out grid over-voltage derating and
inverter throttling needed AC voltage and the inverter status word.

Register numbers below are SolarEdge's documented SunSpec map and were
verified against the live inverter, including two traps:
  - frequency is UNSIGNED; decoding it as int16 wraps 60012 to -5524
  - the power-control block (0xF000+) simply times out when advanced power
    control is disabled, rather than returning a Modbus exception

These inverters historically accept only one Modbus TCP client at a time,
so the connection is opened per read and closed immediately.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

# doc register -> offset from the block base we read
BLOCK_BASE = 40069
BLOCK_LEN = 42


def _s16(v: int) -> int:
    return v - 65536 if v > 32767 else v


def _scaled(raw: int, sf: int, signed: bool = True) -> float:
    value = _s16(raw) if signed else raw
    return value * (10 ** _s16(sf))


def _read_block(host: str, port: int, timeout: int) -> list[int] | str:
    """Raw register block, or a short reason string on failure."""
    from pymodbus.client import ModbusTcpClient

    client = ModbusTcpClient(host, port=port, timeout=timeout)
    try:
        if not client.connect():
            return "connect failed"
        try:
            rr = client.read_holding_registers(BLOCK_BASE, count=BLOCK_LEN, device_id=1)
        except TypeError:  # older pymodbus keyword
            rr = client.read_holding_registers(BLOCK_BASE, count=BLOCK_LEN, slave=1)
        if rr.isError():
            return f"modbus error: {rr}"
        return rr.registers
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    finally:
        client.close()


class ModbusUnavailable(Exception):
    """The inverter could not be read; the message says why."""


def read_inverter(host: str, port: int = 1502, timeout: int = 8,
                  attempts: int = 3) -> dict:
    """One snapshot of the inverter; raises ModbusUnavailable if unreachable.

    The inverter sits on Wi-Fi and its latency swings 0.1-2 s; about a third
    of single-shot reads failed on 2026-09-25, silently. So retry a couple of
    times, and raise with the reason so the caller can record the gap.
    """
    try:
        import pymodbus  # noqa: F401
    except ImportError as exc:
        raise ModbusUnavailable("pymodbus not installed") from exc

    reason = ""
    for attempt in range(attempts):
        if attempt:
            time.sleep(2 * attempt)
        result = _read_block(host, port, timeout)
        if isinstance(result, list):
            regs = result
            break
        reason = result
    else:
        raise ModbusUnavailable(f"failed after {attempts} attempts: {reason}")

    def g(doc: int) -> int:
        return regs[doc - (BLOCK_BASE + 1)]

    ac_power = _scaled(g(40084), g(40085))
    dc_power = _scaled(g(40101), g(40102))
    return {
        "ac_voltage": _scaled(g(40080), g(40083)),
        "ac_current": _scaled(g(40072), g(40076)),
        "ac_power": ac_power,
        # Unsigned: 60012 read as int16 becomes -5524.
        "ac_frequency": _scaled(g(40086), g(40087), signed=False),
        "dc_voltage": _scaled(g(40099), g(40100)),
        "dc_current": _scaled(g(40097), g(40098)),
        "dc_power": dc_power,
        "temperature": _scaled(g(40104), g(40107)),
        # 1 Off, 2 Sleeping, 3 Starting, 4 MPPT/normal, 5 Throttled,
        # 6 Shutting down, 7 Fault, 8 Standby. 5 and 7 are what we're
        # watching for — a limit or fault that only appears intermittently.
        "status": g(40108),
        "status_vendor": g(40109),
        "lifetime_wh": (g(40094) << 16) | g(40095),
        "efficiency_pct": round(100 * ac_power / dc_power, 2) if dc_power else None,
    }


STATUS_NAMES = {
    1: "Off", 2: "Sleeping", 3: "Starting", 4: "MPPT (normal)",
    5: "THROTTLED", 6: "Shutting down", 7: "FAULT", 8: "Standby",
}

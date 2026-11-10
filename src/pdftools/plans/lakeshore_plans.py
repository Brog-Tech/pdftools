"""Debugging and commissioning plans for a Lake Shore 336 and `Cryostat`.

None of these use a detector. They are ordinary bluesky plans, so they can
be run with ``RE(...)`` or queued alongside legacy xpdacq.
"""

from collections.abc import Mapping
from statistics import fmean, pstdev
from typing import Any

import bluesky.plan_stubs as bps
import bluesky.preprocessors as bpp
from bluesky.utils import MsgGenerator
from ophyd_async.core import SignalR

from pdftools.lakeshore import (
    Cryostat,
    Lakeshore336,
    Lakeshore336Loop,
    Lakeshore336RangeSelect,
    Lakeshore336Switch,
)
from pdftools.plans.cryostat_plans import _inputs

Plan = MsgGenerator


def _as_loop(device: Lakeshore336Loop | Cryostat) -> Lakeshore336Loop:
    return device.loop if isinstance(device, Cryostat) else device


def _input_signals(lake: Lakeshore336) -> list[SignalR]:
    return [
        sig
        for _, inp in _inputs(lake)
        for sig in (inp.temp, inp.tempc, inp.raw, inp.status)
    ]


def _loop_signals(loop: Lakeshore336Loop) -> list[SignalR]:
    return [
        loop.enbl,
        loop.loop_mode,
        loop.loop_in_rb,
        loop.sp,
        loop.sprdk,
        loop.p,
        loop.i,
        loop.d,
        loop.ramp_enbl,
        loop.ramp,
        loop.range,
        loop.maxi,
        loop.resistance,
        loop.tolerance,
        loop.settle_time,
        loop.error_on_move_timeout,
    ]


def _print_table(title: str, rows: dict[str, Any]) -> None:
    print(f"\n{title}")
    width = max((len(k) for k in rows), default=0)
    for key, value in rows.items():
        print(f"  {key:<{width}}  {value}")


def lakeshore_snapshot(
    lake: Lakeshore336,
    *loops: Lakeshore336Loop | Cryostat,
    md: dict | None = None,
) -> MsgGenerator[dict[str, Any]]:
    """Record and print the full state of the controller in one event.

    Reads every input (K, °C, raw sensor value and status) and, for each
    loop or cryostat passed, the setpoint, PIDs, ramp, heater range and
    settings, input selection and move settings. Use it before a user run to
    check the controller is set up as expected.

    Returns the readings as ``{signal name: value}``.

    Examples
    --------
    ::

        RE(lakeshore_snapshot(lake, cryostat))
    """
    signals = _input_signals(lake)
    for loop in loops:
        signals += _loop_signals(_as_loop(loop))

    _md = {"plan_name": "lakeshore_snapshot", "purpose": "lakeshore debugging"}
    _md.update(md or {})

    values: dict[str, Any] = {}

    # run_decorator returns the run uid, so collect the readings here.
    @bpp.run_decorator(md=_md)
    def inner() -> Plan:
        readings: Mapping[str, Any] = yield from bps.trigger_and_read(signals)
        values.update({name: r["value"] for name, r in readings.items()})

    yield from inner()
    _print_table(f"{lake.name} snapshot", values)
    return values


def watch_temperatures(
    lake: Lakeshore336,
    duration: float,
    *,
    period: float = 1.0,
    extra: tuple[Any, ...] = (),
    md: dict | None = None,
) -> Plan:
    """Read every input's temperature every `period` seconds for `duration`.

    For drift, noise and cooldown checks without a detector. The readings go
    to the primary stream, so BestEffortCallback plots them live.

    Parameters
    ----------
    lake
        The controller whose inputs to read.
    duration
        How long to watch, in seconds.
    period
        Seconds between readings.
    extra
        More readables to include, e.g. ``(cryostat.loop.sp,)``.

    Examples
    --------
    ::

        RE(watch_temperatures(lake, 600, period=5))
    """
    readables = [inp.temp for _, inp in _inputs(lake)] + list(extra)
    n = max(1, round(duration / period))
    _md = {
        "plan_name": "watch_temperatures",
        "purpose": "lakeshore debugging",
        "duration": duration,
        "period": period,
    }
    _md.update(md or {})

    @bpp.run_decorator(md=_md)
    def inner() -> Plan:
        for i in range(n):
            if i:
                yield from bps.sleep(period)
            yield from bps.trigger_and_read(readables)

    return (yield from inner())


def stability_test(
    cryostat: Cryostat,
    T: float,
    duration: float,
    *,
    period: float = 1.0,
    md: dict | None = None,
) -> MsgGenerator[dict[str, float]]:
    """Move to `T`, then measure how stable the temperature is for `duration`.

    Use it to choose `tolerance` and `settle_time` for a sample environment.
    The move is not recorded; the hold is one run reading the control
    temperature every `period` seconds. Prints and returns the mean,
    standard deviation, peak-to-peak and largest offset from `T`, in K.

    Examples
    --------
    ::

        RE(stability_test(cryostat, 100, 600))
    """
    yield from bps.mv(cryostat, T)

    temp = cryostat.loop_input.temp
    readables = [temp, cryostat.loop.sp]
    n = max(1, round(duration / period))
    values: list[float] = []
    _md = {
        "plan_name": "stability_test",
        "purpose": "cryostat debugging",
        "setpoint": T,
        "duration": duration,
        "period": period,
    }
    _md.update(md or {})

    @bpp.run_decorator(md=_md)
    def inner() -> Plan:
        for i in range(n):
            if i:
                yield from bps.sleep(period)
            readings = yield from bps.trigger_and_read(readables)
            values.append(readings[temp.name]["value"])

    yield from inner()
    stats = {
        "mean": fmean(values),
        "std": pstdev(values),
        "peak_to_peak": max(values) - min(values),
        "max_offset": max(abs(v - T) for v in values),
    }
    _print_table(
        f"{cryostat.name} at {T} K over {duration} s ({len(values)} readings)",
        {k: f"{v:.4f} K" for k, v in stats.items()},
    )
    return stats


def safe_shutdown(
    cryostat: Cryostat | Lakeshore336Loop,
    park_T: float | None = None,
) -> Plan:
    """Turn the heater and loop off, optionally after moving to `park_T`.

    With `park_T` the device is moved there first (honouring its usual
    tolerance and timeout settings), then the heater range is set to OFF and
    the loop is disabled. Safe to run more than once.

    Examples
    --------
    ::

        RE(safe_shutdown(cryostat, park_T=300))
    """
    loop = _as_loop(cryostat)
    if park_T is not None:
        yield from bps.mv(cryostat, park_T)
    yield from bps.mv(loop.range, Lakeshore336RangeSelect.OFF)
    yield from bps.mv(loop.enbl, Lakeshore336Switch.OFF)
    print(f"{loop.name}: heater off, loop disabled")


__all__ = [
    "lakeshore_snapshot",
    "safe_shutdown",
    "stability_test",
    "watch_temperatures",
]

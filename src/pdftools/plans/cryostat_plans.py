"""Temperature-dependent measurement plans for a `Cryostat`.

The measurement plans here are ordinary bluesky plans: run them directly with
``RE(Tlist(...))`` or via legacy xpdacq with ``xrun(sample, Tlist(...))``.
``temperature_signals`` returns a signal list, and ``monitor_temperatures``
returns a preprocessor; neither is itself a plan to pass to ``RE``.

Compatibility with legacy xpdacq:

- Arguments follow xpdacq's order: detectors, exposure, then temperatures.
- If no temperature device is passed, ``xpd_configuration["temp_controller"]``
  is used, as in xpdacq's own ``Tramp`` and ``Tlist``.
- When xpdacq is importable, the area detector is configured for `exposure`
  with xpdacq's ``_configure_area_det``, so frame counting matches legacy
  scans. Without xpdacq the detectors are used as they are.
- The start document carries xpdacq-style ``sp_*`` keys and the legacy
  ``cryostat_*`` / ``Temperature`` keys read by ``meta_data.py``.

xpdacq is imported only when needed; it is not a dependency of pdftools.
"""

from collections.abc import Callable, Iterable, Sequence
from typing import Any

import bluesky.plan_stubs as bps
import bluesky.preprocessors as bpp
import numpy as np
from bluesky.protocols import Readable
from bluesky.utils import MsgGenerator
from ophyd_async.core import Device, SignalR

from pdftools.lakeshore import (
    Cryostat,
    Lakeshore336,
    Lakeshore336Loop,
    Lakeshore336LoopMode,
    Lakeshore336Switch,
)

Plan = MsgGenerator
LEGACY_CHANNELS = ("A", "B", "C", "D")


# --------------------------------------------------------------------- helpers


def _temp_controller(temp_controller: Any | None) -> Any:
    """Return `temp_controller`, or xpdacq's configured one if it is None."""
    if temp_controller is not None:
        return temp_controller
    try:
        from xpdacq.xpdacq_conf import xpd_configuration
    except ImportError as e:
        raise ValueError(
            "No temp_controller given and xpdacq is not available to look one up"
        ) from e
    try:
        return xpd_configuration["temp_controller"]
    except KeyError as e:
        raise ValueError(
            'No temp_controller given and xpd_configuration["temp_controller"] '
            "is not set"
        ) from e


def temperature_signals(*devices: Device | SignalR) -> list[SignalR]:
    """Return the signals worth monitoring for each temperature device.

    - `Cryostat` or `Lakeshore336Loop`: the control input's temperature and
      the loop setpoint.
    - `Lakeshore336`: the temperature of every input.
    - A signal: itself.

    For repeated temperature readings, use
    ``RE(watch_temperatures(lake, 600, period=5))`` instead.
    """
    signals: list[SignalR] = []
    for device in devices:
        if isinstance(device, Cryostat):
            device = device.loop
        if isinstance(device, Lakeshore336Loop):
            signals += [device.loop_input.temp, device.sp]
        elif isinstance(device, Lakeshore336):
            signals += [child.temp for _, child in _inputs(device)]
        elif isinstance(device, SignalR):
            signals.append(device)
        else:
            raise TypeError(f"Don't know which temperatures to monitor on {device}")
    return signals


def monitor_temperatures(*devices: Device | SignalR) -> Callable[[Plan], Plan]:
    """Make a preprocessor that monitors temperatures for the whole plan.

    Usable on any plan, or with legacy xpdacq so every capture is monitored::

        xrun.preprocessors.append(monitor_temperatures(cryostat))
    """
    signals = temperature_signals(*devices)

    def preprocessor(plan: Plan) -> Plan:
        return (yield from bpp.monitor_during_wrapper(plan, signals))

    return preprocessor


def set_T(temp_controller: Any, T: float, *, md: dict | None = None) -> Plan:
    """Move to `T` inside its own short run, so the approach is recorded.

    The temperature and setpoint are monitored during the move.
    """
    _md = {"purpose": "temperature change", "setpoint": T, "plan_name": "set_T"}
    _md.update(md or {})
    plan = bpp.run_wrapper(bps.mv(temp_controller, T), md=_md)
    return (yield from monitor_temperatures(temp_controller)(plan))


def configure_cryostat(cryostat: Cryostat) -> Plan:
    """Set the controller up for `cryostat` and turn the heater on.

    Run this once before the first move: ``RE(configure_cryostat(cryostat))``.
    It:

    - sets the setpoint to the current temperature, so nothing moves when the
      heater comes on;
    - selects the cryostat's input as the loop's sensor;
    - turns the loop on in PID mode with ramping at the cryostat's ``ramp``
      K/min;
    - writes the heater maximum current and resistance;
    - sets the heater range last, which turns the heater on.
    """
    loop = cryostat.loop
    curr_temp = yield from bps.rd(cryostat.loop_input.temp)
    yield from bps.mv(loop.sp, curr_temp)
    yield from bps.mv(
        loop.loop_in_sel,
        loop.loop_input.channel,
        loop.ramp_enbl,
        Lakeshore336Switch.ON,
        loop.ramp,
        cryostat.ramp,
        loop.enbl,
        Lakeshore336Switch.ON,
        loop.loop_mode,
        Lakeshore336LoopMode.PID,
        loop.maxi,
        cryostat.max_current,
        loop.resistance,
        cryostat.resistance,
    )
    # Heater on last.
    yield from bps.mv(loop.range, cryostat.heater_range)


def _inputs(lake: Lakeshore336) -> list[tuple[str, Any]]:
    """(channel letter, input device) for each input `lake` has."""
    return [
        (ch, getattr(lake, f"input_{ch.lower()}"))
        for ch in LEGACY_CHANNELS
        if hasattr(lake, f"input_{ch.lower()}")
    ]


def legacy_md(temp_controller: Any) -> Plan:
    """Read the legacy ``cryostat_*`` metadata keys from a `Cryostat`.

    Returns (as the plan's return value) the same keys as the old
    ``measurement_data()`` / ``read_lakeshore()`` script helpers:
    ``cryostat_A_P/I/D``, ``setpoint``, ``cryostat_<ch>`` and
    ``cryostat_<ch>_V`` for each input, and ``Temperature`` (the control
    input). Returns ``{}`` for any other temperature device.
    """
    if not isinstance(temp_controller, Cryostat):
        return {}
    loop = temp_controller.loop
    md: dict[str, Any] = {
        "cryostat_A_P": (yield from bps.rd(loop.p)),
        "cryostat_A_I": (yield from bps.rd(loop.i)),
        "cryostat_A_D": (yield from bps.rd(loop.d)),
        "setpoint": (yield from bps.rd(loop.sp)),
    }
    lake = temp_controller.loop_input.parent
    if isinstance(lake, Lakeshore336):
        for ch, inp in _inputs(lake):
            md[f"cryostat_{ch}"] = yield from bps.rd(inp.temp)
            md[f"cryostat_{ch}_V"] = yield from bps.rd(inp.raw)
    md["Temperature"] = yield from bps.rd(temp_controller.loop_input.temp)
    return md


def _configure_exposure(exposure: float) -> Plan:
    """Configure the area detector for `exposure` the way xpdacq does.

    Returns the extra ``sp_*`` metadata. A no-op (only
    ``sp_requested_exposure``) when xpdacq is not available.
    """
    md: dict[str, Any] = {"sp_requested_exposure": exposure}
    try:
        from xpdacq.beamtime import _configure_area_det
    except ImportError:
        return md
    num_frame, acq_time, computed_exposure = yield from _configure_area_det(exposure)
    md.update(
        sp_time_per_frame=acq_time,
        sp_num_frames=num_frame,
        sp_computed_exposure=computed_exposure,
    )
    return md


def _start_md(temp_controller: Any, exposure: float, sp: dict, md: dict | None) -> Plan:
    """Configure the detector and build the start-document metadata."""
    _md = yield from _configure_exposure(exposure)
    _md.update(sp)
    _md.update((yield from legacy_md(temp_controller)))
    _md["temp_controller"] = getattr(temp_controller, "name", str(temp_controller))
    _md.update(md or {})
    return _md


def _readables(dets: Iterable[Readable], temp_controller: Any) -> list[Readable]:
    readables = list(dets)
    if temp_controller not in readables:
        readables.append(temp_controller)
    return readables


# ------------------------------------------------------------------- the plans


def Tlist(
    dets: Sequence[Readable],
    exposure: float,
    T_list: Sequence[float],
    *,
    temp_controller: Any | None = None,
    wait: float = 0.0,
    repeat: int = 1,
    md: dict | None = None,
) -> Plan:
    """Measure at each temperature in `T_list`.

    At each temperature: move there (the move finishes once the temperature
    has settled), wait `wait` seconds, then take `repeat` exposures of
    `dets`. Every event includes the temperature. The temperatures are
    monitored throughout.

    Parameters
    ----------
    dets
        Detectors to read, e.g. ``[lambda1]``.
    exposure
        Total exposure per point, in seconds. With xpdacq loaded this is
        split into frames as xpdacq does.
    T_list
        Temperatures in K, in the order to visit them.
    temp_controller
        The temperature device, usually a `Cryostat`. Defaults to xpdacq's
        ``xpd_configuration["temp_controller"]``.
    wait
        Extra seconds to wait after each move before measuring.
    repeat
        Exposures to take at each temperature.
    md
        Extra start-document metadata.

    Examples
    --------
    ::

        RE(Tlist([lambda1], 30, [100, 150, 200], temp_controller=cryostat))
        xrun(0, Tlist([pe1c], 30, [100, 150, 200]))  # legacy xpdacq
    """
    tc = _temp_controller(temp_controller)
    T_list = list(T_list)
    _md = yield from _start_md(
        tc,
        exposure,
        {
            "sp_type": "Tlist",
            "sp_plan_name": "Tlist",
            "sp_T_list": T_list,
            "sp_startingT": T_list[0] if T_list else None,
            "sp_endingT": T_list[-1] if T_list else None,
            "sp_Nsteps": len(T_list),
            "sp_repeat": repeat,
            "sp_wait": wait,
        },
        md,
    )
    readables = _readables(dets, tc)

    @bpp.run_decorator(md=_md)
    def inner() -> Plan:
        for T in T_list:
            yield from bps.mv(tc, T)
            if wait:
                yield from bps.sleep(wait)
            for _ in range(repeat):
                yield from bps.trigger_and_read(readables)

    return (yield from monitor_temperatures(tc)(inner()))


def Tramp(
    dets: Sequence[Readable],
    exposure: float,
    Tstart: float,
    Tstop: float,
    Tstep: float,
    *,
    temp_controller: Any | None = None,
    wait: float = 0.0,
    repeat: int = 1,
    md: dict | None = None,
) -> Plan:
    """Measure from `Tstart` to `Tstop` in steps of `Tstep`, including `Tstop`.

    A `Tlist` with evenly spaced temperatures and xpdacq's ``Tramp``
    metadata. Works heating or cooling; the sign of `Tstep` is taken from
    the direction of travel.

    Parameters
    ----------
    dets, exposure, temp_controller, wait, repeat, md
        As for `Tlist`.
    Tstart, Tstop
        First and last temperatures, in K.
    Tstep
        Step size, in K.

    Examples
    --------
    ::

        RE(Tramp([lambda1], 30, 300, 100, 10, temp_controller=cryostat))
    """
    if Tstep == 0:
        raise ValueError("Tstep must not be 0")
    n = int(round(abs(Tstop - Tstart) / abs(Tstep))) + 1
    T_list = [float(T) for T in np.linspace(Tstart, Tstop, n)]
    _md = {
        "sp_type": "Tramp",
        "sp_plan_name": "Tramp",
        "sp_startingT": Tstart,
        "sp_endingT": Tstop,
        "sp_requested_Tstep": Tstep,
        "sp_Nsteps": n,
    }
    _md.update(md or {})
    return (
        yield from Tlist(
            dets,
            exposure,
            T_list,
            temp_controller=temp_controller,
            wait=wait,
            repeat=repeat,
            md=_md,
        )
    )


def Tramp_continuous(
    dets: Sequence[Readable],
    exposure: float,
    Tstart: float,
    Tstop: float,
    rate: float,
    *,
    temp_controller: Cryostat | None = None,
    md: dict | None = None,
) -> Plan:
    """Measure continuously while the cryostat ramps from `Tstart` to `Tstop`.

    Moves to `Tstart` and lets it settle (before the run opens), sets the
    controller's ramp rate to `rate`, then starts the move to `Tstop` and
    takes exposures back-to-back until the move finishes. Each event records
    the temperature at that exposure. The previous ramp rate is put back
    afterwards, even if the plan fails.

    Parameters
    ----------
    dets, exposure, md
        As for `Tlist`.
    Tstart, Tstop
        Start and end temperatures, in K.
    rate
        Ramp rate in K/min during the measurement.
    temp_controller
        A `Cryostat`. Defaults to xpdacq's
        ``xpd_configuration["temp_controller"]``.

    Examples
    --------
    ::

        RE(Tramp_continuous([lambda1], 10, 100, 300, 2.0, temp_controller=cryostat))
    """
    tc = _temp_controller(temp_controller)
    if not isinstance(tc, Cryostat):
        raise TypeError(f"Tramp_continuous needs a Cryostat, not {tc!r}")
    if rate <= 0:
        raise ValueError("rate must be positive")

    yield from bps.mv(tc, Tstart)
    old_rate = yield from bps.rd(tc.loop.ramp)

    _md = yield from _start_md(
        tc,
        exposure,
        {
            "sp_type": "Tramp_continuous",
            "sp_plan_name": "Tramp_continuous",
            "sp_startingT": Tstart,
            "sp_endingT": Tstop,
            "sp_ramp_rate": rate,
        },
        md,
    )
    readables = _readables(dets, tc)

    @bpp.run_decorator(md=_md)
    def inner() -> Plan:
        yield from bps.mv(tc.loop.ramp, rate)
        status = yield from bps.abs_set(tc, Tstop, group="ramp")
        while not status.done:
            yield from bps.trigger_and_read(readables)
        yield from bps.wait(group="ramp")

    def restore_rate() -> Plan:
        yield from bps.mv(tc.loop.ramp, old_rate)

    return (
        yield from bpp.finalize_wrapper(
            monitor_temperatures(tc)(inner()), restore_rate()
        )
    )

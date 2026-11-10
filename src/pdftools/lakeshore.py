import asyncio
from bisect import bisect
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cached_property
from math import isclose
from typing import Annotated as A

from ophyd_async.core import (
    DeviceMock,
    MovableLogic,
    Reference,
    SignalR,
    SignalRW,
    StandardMovable,
    StandardReadable,
    StrictEnum,
    SubsetEnum,
    TimeoutCalculator,
    callback_on_mock_put,
    default_mock_class,
    derived_signal_r,
    set_mock_value,
    soft_signal_rw,
    wait_for_value,
)
from ophyd_async.core import (
    StandardReadableFormat as Format,
)
from ophyd_async.epics.core import EpicsDevice, PvSuffix


class Lakeshore336Switch(StrictEnum):
    OFF = "OFF"
    ON = "ON"


class Lakeshore336LoopMode(StrictEnum):
    OFF = "OFF"
    PID = "PID"
    ZONE = "ZONE"
    OLOOP = "OPEN LOOP"
    MON = "MONITOR"
    WARM = "WARMUP"


class Lakeshore336RangeSelect(SubsetEnum):
    OFF = "OFF"
    RANGE1 = "RANGE 1"
    RANGE2 = "RANGE 2"
    RANGE3 = "RANGE 3"


class Lakeshore336Resistance(StrictEnum):
    LO25 = "25 OHM"
    HI50 = "50 OHM"


class Lakeshore336LoopInput(SubsetEnum):
    NONE = "NONE"
    A = "A"
    B = "B"
    C = "C"
    D = "D"
    # D2 = "D2"
    # D3 = "D3"
    # D4 = "D4"
    # D5 = "D5"


ALL_INPUTS = tuple(
    channel
    for channel in Lakeshore336LoopInput
    if channel != Lakeshore336LoopInput.NONE
)

LOOP_NUMBERS = (1, 2, 3, 4)

# (thresholds, pids): pids[bisect(thresholds, target)] is used, so pids must have
# one more entry than thresholds.
PIDTable = tuple[Sequence[float], Sequence[tuple[float, float, float]]]


class Lakeshore336Input(EpicsDevice, StandardReadable):
    """One temperature sensor input (A, B, C or D) on a Lake Shore 336.

    `Lakeshore336` creates one per channel as `input_a`, `input_b`, ...
    Read `temp` for the temperature in kelvin.

    Signals
    -------
    temp
        Temperature in kelvin (hinted, so it shows in LiveTable/plots).
    tempc
        Temperature in degrees Celsius.
    raw
        Raw sensor reading (volts or ohms, depending on the sensor type).
    status
        Sensor status string from the controller (e.g. reading errors).

    Parameters
    ----------
    prefix
        Full PV prefix for this channel, e.g. ``"XF:28ID1-ES{LS336:1-Chan:A}"``.
    channel
        Which input this is. This is the input's own identity and does not
        change if a control loop is later pointed at a different input.
    name
        Device name. Usually set by the parent `Lakeshore336`.
    """

    temp: A[SignalR[float], PvSuffix("T-I"), Format.HINTED_SIGNAL]
    tempc: A[SignalR[float], PvSuffix("T:C-I")]
    raw: A[SignalR[float], PvSuffix("Val:Sens-I")]
    status: A[SignalR[str], PvSuffix("T-Sts")]

    def __init__(
        self,
        prefix: str,
        channel: Lakeshore336LoopInput,
        *,
        name: str = "",
    ) -> None:
        self.channel = channel
        super().__init__(prefix, name=name)


def _in_tolerance(
    readback: SignalR[float], target: SignalR[float], tolerance: SignalR[float]
) -> SignalR[bool]:
    """True while `readback` is within `tolerance` of `target`.

    Judged against a local `target` signal rather than the EPICS setpoint, which
    echoes back over a monitor.
    """

    def in_tolerance(readback: float, target: float, tolerance: float) -> bool:
        return isclose(readback, target, rel_tol=0.0, abs_tol=tolerance)

    return derived_signal_r(
        in_tolerance, readback=readback, target=target, tolerance=tolerance
    )


@dataclass
class Lakeshore336LoopLogic(MovableLogic[float]):
    """Generic move for a Lake Shore control loop: write the setpoint and wait for
    `in_tolerance` to hold for `settle_time` seconds.

    If the move times out and `error_on_move_timeout` is False, a warning is
    printed and the move finishes without error, so the plan carries on.
    """

    target: SignalRW[float]
    in_tolerance: SignalR[bool]
    settle_time: SignalR[float]
    move_timeout: float | None
    error_on_move_timeout: SignalR[bool]

    async def calculate_timeout(
        self,
        old_position: float,
        new_position: float,
    ) -> float | None:
        return self.move_timeout

    async def _stays_in_tolerance(self, settle_time: float) -> bool:
        """True if `in_tolerance` does not go False during `settle_time`."""

        left = asyncio.create_task(wait_for_value(self.in_tolerance, False, None))
        try:
            # With a timeout, asyncio.wait returns the unfinished set, not an error.
            done, _ = await asyncio.wait({left}, timeout=settle_time)
        finally:
            left.cancel()
        if done:
            left.result()  # surface a real error from the signal, if any
            return False
        return True

    async def move(self, new_position: float, timeout: TimeoutCalculator) -> None:
        """Write the setpoint and wait for readback to settle around it.

        Raises ``TimeoutError`` on timeout only if `error_on_move_timeout` is
        True; otherwise prints a warning and returns.
        """
        await self.target.set(new_position)
        await self.setpoint.set(new_position)

        try:
            await self._settle(timeout)
        except TimeoutError:
            if await self.error_on_move_timeout.get_value():
                raise
            current = await self.readback.get_value()
            print(
                f"Warning: timed out moving to {new_position:.4f} "
                f"(currently {current:.4f}), continuing..."
            )

    async def _settle(self, timeout: TimeoutCalculator) -> None:
        settle_time = await self.settle_time.get_value()
        async with asyncio.timeout(timeout()):
            while True:
                await wait_for_value(self.in_tolerance, True, timeout=None)
                if await self._stays_in_tolerance(settle_time):
                    return


class Lakeshore336LoopMock(DeviceMock["Lakeshore336Loop"]):
    """Mock behaviour where the control input arrives instantly at the setpoint."""

    async def connect(self, device: "Lakeshore336Loop") -> None:
        def _instant_move(value: float) -> None:
            set_mock_value(device.loop_input.temp, value)

        callback_on_mock_put(device.sp, _instant_move)


@default_mock_class(Lakeshore336LoopMock)
class Lakeshore336Loop(EpicsDevice, StandardReadable, StandardMovable[float]):
    """One heater output / control loop (1-4) on a Lake Shore 336.

    Build it with `Lakeshore336.create_loop()` rather than calling this class
    directly; that checks the loop number and makes sure only one device
    drives each heater output.

    The loop is movable. ``bps.mv(loop, 300)`` writes 300 K to the setpoint,
    then waits until the temperature of `loop_input` stays within `tolerance`
    of 300 K for `settle_time` seconds. If that does not happen within
    `move_timeout` seconds, the move either raises ``TimeoutError`` or, by
    default, prints a warning and lets the plan carry on (see
    `error_on_move_timeout`). ``bps.rd(loop)`` returns the temperature of
    `loop_input`.

    This class only moves the setpoint. It does not turn the heater on or set
    PIDs or ramp rates for you. For that, wrap it in a `Cryostat`, or set the
    signals below yourself.

    Signals
    -------
    sp, sprdk
        Temperature setpoint (K) and its readback from the controller. Both are
        recorded as configuration; the loop's reading is the temperature of
        `loop_input`.
    p, i, d
        PID gains.
    ramp, ramp_enbl
        Setpoint ramp rate (K/min) and whether ramping is on.
    range
        Heater range. ``OFF`` means the heater is off.
    maxi, resistance
        Heater maximum current (A) and heater resistance setting.
    enbl, loop_mode
        Loop on/off and control mode (PID, zone, open loop, ...).
    loop_in_sel, loop_in_rb
        The sensor input the controller uses for this loop (set and readback).
        Call `select_input()` to make the hardware match `loop_input`.
    autotune
        Controller autotune mode.
    tolerance, settle_time
        Soft signals used to decide when a move has finished. You can change
        them at any time, e.g. ``bps.mv(loop.tolerance, 0.5)``.
    error_on_move_timeout
        Soft signal: whether a move that times out raises ``TimeoutError``
        (True) or prints a warning and lets the plan carry on (False). Change
        it at any time with ``bps.mv(loop.error_on_move_timeout, True)``; the
        new value applies from the next timeout. Recorded as configuration.
    target, in_tolerance
        The temperature of the current move and whether `loop_input` is within
        `tolerance` of it. Used internally by moves.

    Parameters
    ----------
    prefix
        PV prefix for this output, e.g. ``"XF:28ID1-ES{LS336:1-Out:1}"``.
    loop_input
        The `Lakeshore336Input` whose temperature this loop controls and
        settles on. It stays a child of the parent `Lakeshore336`.
    tolerance
        Starting value of `tolerance`, in K.
    settle_time
        Starting value of `settle_time`, in seconds.
    move_timeout
        Seconds a move may take before it times out. ``None`` waits forever.
    error_on_move_timeout
        Starting value of `error_on_move_timeout`. Default False (warn and
        carry on). A `Cryostat` built on this loop uses the same signal.
    name
        Device name.
    """

    enbl: A[
        SignalRW[Lakeshore336Switch], PvSuffix("Enbl-Sel")
    ]  # General on and off for the loop

    sp: A[SignalRW[float], PvSuffix("T-SP"), Format.CONFIG_SIGNAL]

    sprdk: A[SignalR[float], PvSuffix("T-RB"), Format.CONFIG_SIGNAL]

    p: A[
        SignalRW[float],
        PvSuffix(write_suffix="Gain:P-SP", read_suffix="Gain:P-RB"),
        Format.CONFIG_SIGNAL,
    ]
    i: A[
        SignalRW[float],
        PvSuffix(write_suffix="Gain:I-SP", read_suffix="Gain:I-RB"),
        Format.CONFIG_SIGNAL,
    ]
    d: A[
        SignalRW[float],
        PvSuffix(write_suffix="Gain:D-SP", read_suffix="Gain:D-RB"),
        Format.CONFIG_SIGNAL,
    ]
    ramp: A[
        SignalRW[float],
        PvSuffix(write_suffix="Val:Ramp-SP", read_suffix="Val:Ramp-RB"),
        Format.CONFIG_SIGNAL,
    ]

    range: A[
        SignalRW[Lakeshore336RangeSelect],
        PvSuffix(write_suffix="Val:Range-Sel", read_suffix="Val:Range-Sts"),
        Format.CONFIG_SIGNAL,
    ]

    maxi: A[SignalRW[float], PvSuffix("Out:MaxI-SP")]

    loop_mode: A[
        SignalRW[Lakeshore336LoopMode], PvSuffix("Mode-Sel"), Format.CONFIG_SIGNAL
    ]

    resistance: A[
        SignalRW[Lakeshore336Resistance], PvSuffix("Out:R-SP"), Format.CONFIG_SIGNAL
    ]

    ramp_enbl: A[
        SignalRW[Lakeshore336Switch], PvSuffix("Enbl:Ramp-Sel"), Format.CONFIG_SIGNAL
    ]

    autotune: A[
        SignalRW[str],
        PvSuffix(write_suffix="Mode:ATune-Sel", read_suffix="Mode:ATune-Sts"),
    ]

    loop_in_sel: A[
        SignalRW[Lakeshore336LoopInput], PvSuffix("Out-Sel"), Format.CONFIG_SIGNAL
    ]

    loop_in_rb: A[
        SignalR[Lakeshore336LoopInput], PvSuffix("Out-Sts"), Format.CONFIG_SIGNAL
    ]

    def __init__(
        self,
        prefix: str,
        loop_input: Lakeshore336Input,
        *,
        tolerance: float = 0.1,
        settle_time: float = 5.0,
        move_timeout: float | None = 600.0,
        error_on_move_timeout: bool = False,
        name: str = "",
    ) -> None:
        # Wrapped in Reference so `loop_input` (already a child of the parent
        # Lakeshore336) is not re-parented under this loop too.
        self._loop_input_ref = Reference(loop_input)
        self._move_timeout = move_timeout
        with self.add_children_as_readables(Format.CONFIG_SIGNAL):
            self.tolerance = soft_signal_rw(float, initial_value=tolerance)
            self.settle_time = soft_signal_rw(float, initial_value=settle_time)
            self.error_on_move_timeout = soft_signal_rw(
                bool, initial_value=error_on_move_timeout
            )
        self.target = soft_signal_rw(float, initial_value=0.0)
        self.in_tolerance = _in_tolerance(loop_input.temp, self.target, self.tolerance)
        super().__init__(prefix, name=name)
        self.add_readables([self.movable_logic.readback], Format.HINTED_SIGNAL)

    @property
    def loop_input(self) -> Lakeshore336Input:
        """The sensor input this loop controls and settles on."""
        return self._loop_input_ref()

    async def select_input(self) -> None:
        """Tell the controller to use `loop_input` as this loop's sensor.

        The input passed when the loop was built is only known to Python
        until this is called; the controller may still be using another one.
        """
        await self.loop_in_sel.set(self.loop_input.channel)

    async def get_loop_temp_k(self) -> float:
        """Return the current temperature of `loop_input`, in kelvin."""
        return await self.loop_input.temp.get_value()

    @cached_property
    def movable_logic(self) -> MovableLogic[float]:
        """Connect StandardMovable to this loop's setpoint and a private
        mirror of the assigned input's temperature.

        A derived signal is used (rather than `self.loop_input.temp`
        directly) because `StandardMovable.set_name()` renames its
        `readback` signal to match this loop's name. `loop_input.temp` is
        also an independently readable child of the top-level Lakeshore336
        device, so reusing it here would rename it out from under anyone
        reading that channel directly.
        """
        return Lakeshore336LoopLogic(
            setpoint=self.sp,
            readback=_mirror(self.loop_input.temp),
            target=self.target,
            in_tolerance=self.in_tolerance,
            settle_time=self.settle_time,
            move_timeout=self._move_timeout,
            error_on_move_timeout=self.error_on_move_timeout,
        )


def _mirror(signal: SignalR[float]) -> SignalR[float]:
    def temp(temp: float) -> float:
        return temp

    return derived_signal_r(temp, temp=signal)


# TODO Implement this upstream in nslsii
class Lakeshore336(StandardReadable):
    """A Lake Shore 336 temperature controller.

    The controller's sensor inputs are created as children named `input_a`,
    `input_b`, ... and all of their temperatures are read when the controller
    is read (e.g. as a detector in a scan).

    Control loops are not created automatically. Build the one you need with
    `create_loop()` and hand it to the device that drives it, usually a
    `Cryostat`, which then owns it::

        lakeshore = Lakeshore336("XF:28ID1-ES{LS336:1", name="lakeshore")
        cryostat = Cryostat(
            lakeshore.create_loop(1, lakeshore.input_c, tolerance=1.0),
            name="cryostat",
        )

    Parameters
    ----------
    prefix
        PV prefix of the controller without the closing brace, e.g.
        ``"XF:28ID1-ES{LS336:1"``. Channel and output suffixes such as
        ``"-Chan:A}"`` and ``"-Out:1}"`` are added to it.
    inputs
        Which sensor inputs to create. Defaults to all of A-D. Leave out
        channels with no sensor so they are not read.
    name
        Device name.
    """

    # Only the channels passed in `inputs` exist; declared for type checkers.
    input_a: Lakeshore336Input
    input_b: Lakeshore336Input
    input_c: Lakeshore336Input
    input_d: Lakeshore336Input
    input_d2: Lakeshore336Input
    input_d3: Lakeshore336Input
    input_d4: Lakeshore336Input
    input_d5: Lakeshore336Input

    def __init__(
        self,
        prefix: str,
        *,
        inputs: Sequence[Lakeshore336LoopInput] = ALL_INPUTS,
        name: str = "",
    ):
        self._prefix = prefix
        self._claimed_loops: set[int] = set()

        with self.add_children_as_readables():
            for channel in inputs:
                if channel == Lakeshore336LoopInput.NONE:
                    raise ValueError(f"{channel} is not a Lake Shore 336 input")
                setattr(
                    self,
                    f"input_{channel.value.lower()}",
                    Lakeshore336Input(f"{prefix}-Chan:{channel.value}}}", channel),
                )

        super().__init__(name=name)

    def create_loop(
        self,
        number: int,
        loop_input: Lakeshore336Input,
        *,
        tolerance: float = 0.1,
        settle_time: float = 5.0,
        move_timeout: float | None = 600.0,
        error_on_move_timeout: bool = False,
        name: str = "",
    ) -> Lakeshore336Loop:
        """Create the device for control loop `number`, controlling on `loop_input`.

        Each loop number can only be created once per controller, so two
        devices cannot drive the same heater output. Only Python is changed
        here; nothing is written to the controller until the loop is moved or
        configured (see `Lakeshore336Loop.select_input` and
        `pdftools.plans.configure_cryostat`).

        Parameters
        ----------
        number
            Heater output / loop number, 1-4.
        loop_input
            One of this controller's inputs, e.g. ``lakeshore.input_c``.
        tolerance
            How close (K) the temperature must be to the setpoint for a move
            to count as arrived.
        settle_time
            How long (s) the temperature must stay within `tolerance` before
            a move finishes.
        move_timeout
            Seconds a move of the bare loop may take before it fails. ``None``
            waits forever. A `Cryostat` ignores this and works out its own
            timeout from the ramp rate.
        error_on_move_timeout
            If True, a move that times out raises ``TimeoutError`` and stops
            the plan. If False (the default) a warning with the target and
            current temperature is printed and the plan carries on. Applies
            to the loop and to any `Cryostat` built on it. This is the
            starting value of the `error_on_move_timeout` soft signal and can
            be changed later with ``bps.mv(loop.error_on_move_timeout, True)``.
        name
            Device name. Leave empty when passing the loop to a `Cryostat`;
            it is named after the cryostat (e.g. ``"cryostat-loop"``).

        Raises
        ------
        ValueError
            If `number` is not 1-4, the loop was already created, or
            `loop_input` belongs to a different controller.
        """
        if number not in LOOP_NUMBERS:
            raise ValueError(f"Loop number must be one of {LOOP_NUMBERS}, not {number}")
        if number in self._claimed_loops:
            raise ValueError(f"Loop {number} of {self.name} has already been built")
        if loop_input.parent is not self:
            raise ValueError(f"{loop_input.name} is not an input of {self.name}")
        self._claimed_loops.add(number)
        return Lakeshore336Loop(
            f"{self._prefix}-Out:{number}}}",
            loop_input,
            tolerance=tolerance,
            settle_time=settle_time,
            move_timeout=move_timeout,
            error_on_move_timeout=error_on_move_timeout,
            name=name,
        )


@dataclass
class CryostatLogic(Lakeshore336LoopLogic):
    """How a `Cryostat` moves.

    On top of `Lakeshore336LoopLogic` it:

    - refuses targets outside ``[min_temp, max_temp]``;
    - picks PID gains from `heating_pids` or `cooling_pids` by the target
      temperature and the direction of travel, leaving them unchanged for
      moves smaller than `pid_deadband`;
    - works out the timeout from the ramp rate, the settle time and
      `timeout_buffer`.

    Timeouts are handled by the base class according to `error_on_move_timeout`,
    which `Cryostat` takes from its loop.
    """

    p: SignalRW[float]
    i: SignalRW[float]
    d: SignalRW[float]
    ramp: SignalRW[float]
    min_temp: float
    max_temp: float
    pid_deadband: float
    heating_pids: PIDTable
    cooling_pids: PIDTable
    ramp_fallback: float
    timeout_buffer: float

    async def check_move(self, new_position: float) -> None:
        if not self.min_temp <= new_position <= self.max_temp:
            raise ValueError(
                f"{new_position} is outside the cryostat limits "
                f"[{self.min_temp}, {self.max_temp}]"
            )

    async def set_pids(self, delta: float, new_position: float) -> None:
        if delta > self.pid_deadband:
            temp_thresholds, pids = self.heating_pids
        elif delta < -self.pid_deadband:
            temp_thresholds, pids = self.cooling_pids
        else:
            return

        _p, _i, _d = pids[bisect(temp_thresholds, new_position)]
        await asyncio.gather(self.p.set(_p), self.i.set(_i), self.d.set(_d))

    async def calculate_timeout(
        self,
        old_position: float,
        new_position: float,
    ) -> float:

        self._old_pos = old_position
        self._new_pos = new_position

        ramp_rate, settle_time, setpoint = await asyncio.gather(
            self.ramp.get_value(),  # degrees/minute
            self.settle_time.get_value(),
            self.setpoint.get_value(),
        )
        if ramp_rate <= 0:
            ramp_rate = self.ramp_fallback

        delta = new_position - old_position
        spdelta = new_position - setpoint
        print(f"Delta T = {delta:.2f}")
        max_delta = max(abs(delta), abs(spdelta))

        travel_time = abs(max_delta) / ramp_rate * 60
        timeout = travel_time + settle_time + self.timeout_buffer
        print(f"{timeout:.2f} Seconds to complete the temp change")
        return timeout

    async def move(self, new_position: float, timeout: TimeoutCalculator) -> None:
        await self.check_move(new_position)
        original_temp = await self.readback.get_value()
        print(f"Changing temp from {original_temp:.4f} to {new_position:.4f}")
        await self.set_pids(new_position - original_temp, new_position)
        await super().move(new_position, timeout)


@default_mock_class(DeviceMock)
class Cryostat(StandardReadable, StandardMovable[float]):
    """A cryostat whose temperature is controlled by one Lake Shore 336 loop.

    Typical setup::

        lakeshore = Lakeshore336("XF:28ID1-ES{LS336:1", name="lakeshore")
        cryostat = Cryostat(
            lakeshore.create_loop(1, lakeshore.input_c, tolerance=1.0, settle_time=10),
            name="cryostat",
        )
        RE(configure_cryostat(cryostat))  # once, before the first move

    Each move:

    1. checks the target is within ``[min_temp, max_temp]``;
    2. sets PID gains from `heating_pids` or `cooling_pids`, unless the move
       is smaller than `pid_deadband`;
    3. writes the setpoint and waits until the temperature stays within the
       loop's `tolerance` for the loop's `settle_time`;
    4. times out after ``distance / ramp rate + settle_time + timeout_buffer``
       seconds, where ``distance`` is the larger of the change from the current
       temperature and the change from the current setpoint.

    `tolerance`, `settle_time` and `error_on_move_timeout` belong to the loop:
    set their starting values with `Lakeshore336.create_loop()` and change
    them later with e.g. ``bps.mv(cryostat.loop.error_on_move_timeout, True)``.
    With `error_on_move_timeout` False (the default) a move that times out
    prints a warning and the plan carries on; with True it raises
    ``TimeoutError``. All three are recorded in each scan's configuration.

    Parameters
    ----------
    loop
        The control loop to drive, from `Lakeshore336.create_loop()`. The
        cryostat takes ownership of it (it becomes ``cryostat.loop``).
    min_temp, max_temp
        Allowed target range, in K. Moves outside it raise ``ValueError``.
    pid_deadband
        Moves smaller than this (K) keep the current PID gains.
    heating_pids, cooling_pids
        PID tables used when heating and cooling. Each is
        ``(thresholds, pids)`` where ``pids`` has one more entry than
        ``thresholds``. Targets below ``thresholds[0]`` use ``pids[0]``,
        targets between ``thresholds[0]`` and ``thresholds[1]`` use
        ``pids[1]``, and so on.
    ramp_fallback
        Ramp rate (K/min) used to work out the timeout when the controller's
        ramp rate reads 0.
    timeout_buffer
        Extra seconds added to every move's timeout.
    ramp
        Ramp rate (K/min) written by `configure_cryostat()`.
    max_current
        Heater maximum current (A) written by `configure_cryostat()`.
    resistance
        Heater resistance setting written by `configure_cryostat()`.
    heater_range
        Heater range written by `configure_cryostat()`, which turns the heater on.
    name
        Device name.
    """

    def __init__(
        self,
        loop: Lakeshore336Loop,
        *,
        min_temp: float = 5.0,
        max_temp: float = 550.0,
        pid_deadband: float = 0.5,
        heating_pids: PIDTable = (
            (140, 270, 300),
            ((50, 10, 1), (25, 6, 3), (26, 5, 3), (26, 5, 3)),
        ),
        cooling_pids: PIDTable = (
            (140, 200),
            ((25, 4, 3), (25, 6, 3), (35, 8, 3)),
        ),
        ramp_fallback: float = 0.015,
        timeout_buffer: float = 30.0,
        ramp: float = 6.0,
        max_current: float = 2.0,
        resistance: Lakeshore336Resistance = Lakeshore336Resistance.LO25,
        heater_range: Lakeshore336RangeSelect = Lakeshore336RangeSelect.RANGE3,
        name: str = "",
    ) -> None:
        for thresholds, pids in (heating_pids, cooling_pids):
            if len(pids) != len(thresholds) + 1:
                raise ValueError(
                    f"PID table needs one more PID than thresholds: {thresholds}, "
                    f"{pids}"
                )
        self._min_temp = min_temp
        self._max_temp = max_temp
        self._pid_deadband = pid_deadband
        self._heating_pids = heating_pids
        self._cooling_pids = cooling_pids
        self._ramp_fallback = ramp_fallback
        self._timeout_buffer = timeout_buffer
        self.ramp = ramp
        self.max_current = max_current
        self.resistance = resistance
        self.heater_range = heater_range

        self.loop = loop
        self.add_readables([self.movable_logic.readback], Format.HINTED_SIGNAL)
        self.add_readables([loop.sp, loop.sprdk], Format.CONFIG_SIGNAL)
        self.add_readables(
            [
                loop.tolerance,
                loop.settle_time,
                loop.error_on_move_timeout,
                loop.p,
                loop.i,
                loop.d,
                loop.ramp,
                loop.range,
                loop.loop_in_rb,
            ],
            Format.CONFIG_SIGNAL,
        )
        super().__init__(name=name)

    @property
    def loop_input(self) -> Lakeshore336Input:
        """The sensor input the cryostat's temperature is read from."""
        return self.loop.loop_input

    @cached_property
    def movable_logic(self) -> CryostatLogic:
        return CryostatLogic(
            setpoint=self.loop.sp,
            readback=_mirror(self.loop.loop_input.temp),
            target=self.loop.target,
            in_tolerance=self.loop.in_tolerance,
            settle_time=self.loop.settle_time,
            move_timeout=None,
            p=self.loop.p,
            i=self.loop.i,
            d=self.loop.d,
            ramp=self.loop.ramp,
            min_temp=self._min_temp,
            max_temp=self._max_temp,
            pid_deadband=self._pid_deadband,
            heating_pids=self._heating_pids,
            cooling_pids=self._cooling_pids,
            ramp_fallback=self._ramp_fallback,
            timeout_buffer=self._timeout_buffer,
            error_on_move_timeout=self.loop.error_on_move_timeout,
        )

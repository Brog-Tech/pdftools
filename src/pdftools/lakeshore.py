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
    TimeoutCalculator,
    callback_on_mock_put,
    default_mock_class,
    derived_signal_r,
    set_mock_value,
    soft_signal_rw,
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


class Lakeshore336RangeSelect(StrictEnum):
    OFF = "OFF"
    RANGE1 = "RANGE 1"
    RANGE2 = "RANGE 2"
    RANGE3 = "RANGE 3"


class Lakeshore336Resistance(StrictEnum):
    LO25 = "25 OHM"
    HI50 = "50 OHM"


class Lakeshore336LoopInput(StrictEnum):
    NONE = "NONE"
    A = "A"
    B = "B"
    C = "C"
    D = "D"
    D2 = "D2"
    D3 = "D3"
    D4 = "D4"
    D5 = "D5"


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
    """One Lake Shore 336 temperature input."""

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
        """`channel` is this input's own identity (A, B, C, ...), independent of
        whichever loop (if any) is currently reading it as a control input."""
        self.channel = channel
        super().__init__(prefix, name=name)


@dataclass
class Lakeshore336LoopLogic(MovableLogic[float]):
    """Generic move for a Lake Shore control loop: write the setpoint and wait for
    the control input to stay within `tolerance` for `settle_time` seconds."""

    tolerance: SignalR[float]
    settle_time: SignalR[float]
    move_timeout: float | None

    async def calculate_timeout(
        self,
        old_position: float,
        new_position: float,
    ) -> float | None:
        return self.move_timeout

    async def move(self, new_position: float, timeout: TimeoutCalculator) -> None:
        """Write the setpoint and wait for readback to settle around it."""
        tolerance, settle_time = await asyncio.gather(
            self.tolerance.get_value(), self.settle_time.get_value()
        )

        loop = asyncio.get_running_loop()
        settled = asyncio.Event()
        settle_timer: asyncio.TimerHandle | None = None

        def update_settled_state(reading) -> None:
            nonlocal settle_timer
            value = reading[self.readback.name]["value"]
            in_tolerance = isclose(value, new_position, rel_tol=0.0, abs_tol=tolerance)

            if in_tolerance and settle_timer is None:
                # Start a timer the first time the readback enters tolerance.
                # It is cancelled below if a later update leaves tolerance.
                settle_timer = loop.call_later(settle_time, settled.set)
            elif not in_tolerance and settle_timer is not None:
                settle_timer.cancel()
                settle_timer = None
                settled.clear()

        # Subscribe before writing so that a fast readback update cannot be missed.
        self.readback.subscribe_reading(update_settled_state)
        try:
            await self.setpoint.set(new_position)
            async with asyncio.timeout(timeout()):
                await settled.wait()
        finally:
            if settle_timer is not None:
                settle_timer.cancel()
            self.readback.clear_sub(update_settled_state)


class Lakeshore336LoopMock(DeviceMock["Lakeshore336Loop"]):
    """Mock behaviour where the control input arrives instantly at the setpoint."""

    async def connect(self, device: "Lakeshore336Loop") -> None:
        def _instant_move(value: float) -> None:
            set_mock_value(device.loop_input.temp, value)

        callback_on_mock_put(device.sp, _instant_move)


@default_mock_class(Lakeshore336LoopMock)
class Lakeshore336Loop(EpicsDevice, StandardReadable, StandardMovable[float]):
    """
    One heater/control loop.

    setpoint, PID, heater range, ramp settings, etc. Moving the loop writes the
    setpoint and settles on the temperature of `loop_input`.
    """

    enbl: A[
        SignalRW[Lakeshore336Switch], PvSuffix("Enbl-Sel")
    ]  # General on and off for the loop

    sp: A[SignalRW[float], PvSuffix("T-SP"), Format.CONFIG_SIGNAL]

    sprdk: A[SignalR[float], PvSuffix("T-RB"), Format.HINTED_SIGNAL]

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
        name: str = "",
    ) -> None:
        # Wrapped in Reference so `loop_input` (already a child of the parent
        # Lakeshore336) is not re-parented under this loop too.
        self._loop_input_ref = Reference(loop_input)
        self._move_timeout = move_timeout
        with self.add_children_as_readables(Format.CONFIG_SIGNAL):
            self.tolerance = soft_signal_rw(float, initial_value=tolerance)
            self.settle_time = soft_signal_rw(float, initial_value=settle_time)
        super().__init__(prefix, name=name)

    @property
    def loop_input(self) -> Lakeshore336Input:
        return self._loop_input_ref()

    async def select_input(self) -> None:
        """Point the physical loop's PID input at the channel `loop_input`
        was constructed with, so the hardware selection matches the Python
        object graph."""
        await self.loop_in_sel.set(self.loop_input.channel)

    async def get_loop_temp_k(self) -> float:
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
            tolerance=self.tolerance,
            settle_time=self.settle_time,
            move_timeout=self._move_timeout,
        )


def _mirror(signal: SignalR[float]) -> SignalR[float]:
    def temp(temp: float) -> float:
        return temp

    return derived_signal_r(temp, temp=signal)


class Lakeshore336(StandardReadable):
    """Lake Shore 336 controller.

    Creates its inputs as readable children. Control loops are not children:
    they are built on demand with `loop()`, so the device that drives a loop
    (e.g. a `Cryostat`) can own it.
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

    def loop(
        self,
        number: int,
        loop_input: Lakeshore336Input,
        *,
        tolerance: float = 0.1,
        settle_time: float = 5.0,
        move_timeout: float | None = 600.0,
        name: str = "",
    ) -> Lakeshore336Loop:
        """Build control loop `number` (1-4) closed on `loop_input`.

        Each loop can only be built once, so two devices cannot drive the same
        heater output.
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
            name=name,
        )


@dataclass
class CryostatLogic(Lakeshore336LoopLogic):
    """Cryostat move: limit-checked, PIDs scheduled from the target temperature and
    direction of travel, and a timeout calculated from the ramp rate."""

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
        delta = new_position - old_position
        print(f"Delta = {delta}")

        ramp_rate, settle_time = await asyncio.gather(
            self.ramp.get_value(),  # perhaps degrees/minute
            self.settle_time.get_value(),
        )
        if ramp_rate == 0:
            ramp_rate = self.ramp_fallback

        travel_time = abs(delta) / ramp_rate * 60
        timeout = travel_time + settle_time + self.timeout_buffer
        print(f"{timeout} Seconds to complete the temp change")
        return timeout

    async def move(self, new_position: float, timeout: TimeoutCalculator) -> None:
        original_temp = await self.readback.get_value()
        print(f"Changing temp from {original_temp} to {new_position}")
        await self.set_pids(new_position - original_temp, new_position)
        await super().move(new_position, timeout)


@default_mock_class(DeviceMock)
class Cryostat(StandardReadable, StandardMovable[float]):
    """A cryostat driven by one Lake Shore 336 control loop.

    The cryostat owns `loop` (build it with `Lakeshore336.loop()`), and its
    readback is the temperature of the loop's control input, so `bps.rd(cryostat)`
    returns the temperature the loop is controlling on.
    """

    def __init__(
        self,
        loop: Lakeshore336Loop,
        *,
        tolerance: float = 1.0,
        settle_time: float = 120.0,
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
        self._ramp = ramp
        self._max_current = max_current
        self._resistance = resistance
        self._heater_range = heater_range

        self.loop = loop
        with self.add_children_as_readables(Format.CONFIG_SIGNAL):
            self.tolerance = soft_signal_rw(float, initial_value=tolerance)
            self.settle_time = soft_signal_rw(float, initial_value=settle_time)
        self.add_readables([self.movable_logic.readback], Format.HINTED_SIGNAL)
        self.add_readables([loop.sp, loop.sprdk])
        self.add_readables(
            [loop.p, loop.i, loop.d, loop.ramp, loop.range, loop.loop_in_rb],
            Format.CONFIG_SIGNAL,
        )
        super().__init__(name=name)

    @property
    def loop_input(self) -> Lakeshore336Input:
        return self.loop.loop_input

    async def configure(self) -> None:
        """Set the loop up for closed-loop control on `loop_input` and turn the
        heater on."""
        # Set the loop sp to the current temp so nothing moves irradically.
        curr_temp = await self.loop.get_loop_temp_k()
        await self.loop.sp.set(curr_temp)

        await asyncio.gather(
            # Set input as defined in object definition
            self.loop.select_input(),
            self.loop.ramp_enbl.set(Lakeshore336Switch.ON),
            self.loop.ramp.set(self._ramp),
            self.loop.enbl.set(Lakeshore336Switch.ON),
            self.loop.loop_mode.set(Lakeshore336LoopMode.PID),
            self.loop.maxi.set(self._max_current),
            self.loop.resistance.set(self._resistance),
        )

        # Turn on the heater
        await self.loop.range.set(self._heater_range)

    @cached_property
    def movable_logic(self) -> CryostatLogic:
        return CryostatLogic(
            setpoint=self.loop.sp,
            readback=_mirror(self.loop.loop_input.temp),
            tolerance=self.tolerance,
            settle_time=self.settle_time,
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
        )

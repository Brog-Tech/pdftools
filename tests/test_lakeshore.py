import asyncio

import pytest
from bluesky.run_engine import RunEngine
from ophyd_async.core import (
    callback_on_mock_put,
    get_mock_put,
    init_devices,
    set_mock_value,
)

from pdftools.lakeshore import (
    Cryostat,
    Lakeshore336,
    Lakeshore336Loop,
    Lakeshore336LoopInput,
    Lakeshore336LoopMode,
    Lakeshore336RangeSelect,
    Lakeshore336Resistance,
    Lakeshore336Switch,
)
from pdftools.plans import configure_cryostat


@pytest.fixture
async def ls336() -> Lakeshore336:
    async with init_devices(mock=True):
        ls336 = Lakeshore336("Test{LS336:1", name="ls336")
    return ls336


@pytest.fixture
async def cryostat(ls336: Lakeshore336) -> Cryostat:
    async with init_devices(mock=True):
        cryostat = Cryostat(
            ls336.create_loop(1, ls336.input_c, settle_time=0.1), name="cryostat"
        )
    return cryostat


def hold_temperature(loop: Lakeshore336Loop) -> None:
    """Stop the mock control input jumping straight to the setpoint."""
    callback_on_mock_put(loop.sp, lambda value: None)


async def test_lakeshore_builds_requested_inputs():
    async with init_devices(mock=True):
        ls336 = Lakeshore336(
            "LS:",
            inputs=(Lakeshore336LoopInput.A, Lakeshore336LoopInput.B),
            name="ls336",
        )
    assert ls336.input_a.channel == Lakeshore336LoopInput.A
    assert ls336.input_b.name == "ls336-input_b"
    assert not hasattr(ls336, "input_c")


def test_lakeshore_rejects_none_input():
    with pytest.raises(ValueError, match="not a Lake Shore 336 input"):
        Lakeshore336("LS:", inputs=(Lakeshore336LoopInput.NONE,))


async def test_loop_can_only_be_built_once(ls336: Lakeshore336):
    ls336.create_loop(1, ls336.input_c)
    with pytest.raises(ValueError, match="already been built"):
        ls336.create_loop(1, ls336.input_a)
    # Other loops are still available.
    ls336.create_loop(2, ls336.input_a)


async def test_loop_number_and_input_are_validated(ls336: Lakeshore336):
    with pytest.raises(ValueError, match="Loop number"):
        ls336.create_loop(5, ls336.input_a)
    other = Lakeshore336("OTHER:", name="other")
    with pytest.raises(ValueError, match="is not an input of ls336"):
        ls336.create_loop(1, other.input_a)


async def test_loop_prefix(ls336: Lakeshore336):
    loop = ls336.create_loop(3, ls336.input_a)
    assert loop.sp.source == "ca://Test{LS336:1-Out:3}T-SP"


async def test_cryostat_owns_loop_and_references_input(
    ls336: Lakeshore336, cryostat: Cryostat
):
    assert cryostat.loop.parent is cryostat
    assert cryostat.loop.name == "cryostat-loop"
    assert cryostat.loop_input is ls336.input_c
    assert ls336.input_c.parent is ls336
    assert ls336.input_c.temp.name == "ls336-input_c-temp"


async def test_cryostat_readback_is_control_input(
    ls336: Lakeshore336, cryostat: Cryostat
):
    set_mock_value(ls336.input_c.temp, 77.0)
    set_mock_value(cryostat.loop.sp, 300.0)

    location = await cryostat.locate()
    assert location == {"setpoint": 300.0, "readback": 77.0}

    reading = await cryostat.read()
    assert reading["cryostat"]["value"] == 77.0
    assert set(reading) == {"cryostat"}
    assert cryostat.hints == {"fields": ["cryostat"]}

    config = await cryostat.read_configuration()
    assert set(config) == {
        "cryostat-loop-tolerance",
        "cryostat-loop-settle_time",
        "cryostat-loop-error_on_move_timeout",
        "cryostat-loop-sp",
        "cryostat-loop-sprdk",
        "cryostat-loop-p",
        "cryostat-loop-i",
        "cryostat-loop-d",
        "cryostat-loop-ramp",
        "cryostat-loop-range",
        "cryostat-loop-loop_in_rb",
    }


async def test_loop_settles_within_tolerance(ls336: Lakeshore336):
    async with init_devices(mock=True):
        loop = ls336.create_loop(2, ls336.input_a, tolerance=0.5, settle_time=0.2)
    hold_temperature(loop)
    set_mock_value(ls336.input_a.temp, 10.0)

    status = loop.set(20.0)
    await asyncio.sleep(0.05)
    set_mock_value(ls336.input_a.temp, 19.7)  # in tolerance, starts settle timer
    await asyncio.sleep(0.1)
    set_mock_value(ls336.input_a.temp, 21.0)  # leaves tolerance, resets timer
    await asyncio.sleep(0.15)
    assert not status.done
    set_mock_value(ls336.input_a.temp, 20.2)
    await asyncio.sleep(0.1)
    assert not status.done
    await status
    assert await loop.sp.get_value() == 20.0


async def test_loop_in_tolerance_signal_tracks_target(ls336: Lakeshore336):
    async with init_devices(mock=True):
        loop = ls336.create_loop(2, ls336.input_a, tolerance=0.5, settle_time=0.05)
    set_mock_value(loop.target, 20.0)
    set_mock_value(ls336.input_a.temp, 19.7)
    assert await loop.in_tolerance.get_value() is True
    set_mock_value(ls336.input_a.temp, 21.0)
    assert await loop.in_tolerance.get_value() is False
    set_mock_value(loop.tolerance, 2.0)  # tolerance changes are followed
    assert await loop.in_tolerance.get_value() is True


async def test_loop_does_not_settle_on_stale_setpoint(ls336: Lakeshore336):
    """The readback starts in tolerance of the *old* setpoint. The move must be
    judged against the new target, not report settled straight away."""
    async with init_devices(mock=True):
        loop = ls336.create_loop(2, ls336.input_a, tolerance=0.5, settle_time=0.05)
    hold_temperature(loop)
    set_mock_value(loop.sp, 10.0)
    set_mock_value(ls336.input_a.temp, 10.0)

    status = loop.set(50.0)
    await asyncio.sleep(0.2)  # several settle_times with the readback still at 10
    assert not status.done
    assert await loop.in_tolerance.get_value() is False

    set_mock_value(ls336.input_a.temp, 50.1)
    await status


async def test_loop_times_out_when_error_on_move_timeout(ls336: Lakeshore336):
    async with init_devices(mock=True):
        loop = ls336.create_loop(
            2, ls336.input_a, move_timeout=0.1, error_on_move_timeout=True
        )
    hold_temperature(loop)
    with pytest.raises(TimeoutError):
        await loop.set(20.0)


async def test_loop_warns_and_continues_on_timeout_by_default(
    ls336: Lakeshore336, capsys: pytest.CaptureFixture[str]
):
    async with init_devices(mock=True):
        loop = ls336.create_loop(2, ls336.input_a, move_timeout=0.1)
    hold_temperature(loop)
    set_mock_value(ls336.input_a.temp, 5.0)
    await loop.set(20.0)
    assert "timed out moving to 20.0000 (currently 5.0000)" in capsys.readouterr().out
    assert await loop.sp.get_value() == 20.0


async def test_error_on_move_timeout_can_be_changed_at_runtime(
    cryostat: Cryostat,
):
    hold_temperature(cryostat.loop)
    set_mock_value(cryostat.loop_input.temp, 100.0)
    await cryostat.set(200.0, timeout=0.1)  # default: warn and carry on
    await cryostat.loop.error_on_move_timeout.set(True)
    with pytest.raises(TimeoutError):
        await cryostat.set(250.0, timeout=0.1)


@pytest.mark.parametrize("error_on_move_timeout", [True, False])
async def test_cryostat_error_on_move_timeout(
    ls336: Lakeshore336, error_on_move_timeout: bool
):
    async with init_devices(mock=True):
        cryostat = Cryostat(
            ls336.create_loop(
                1,
                ls336.input_c,
                settle_time=0.05,
                error_on_move_timeout=error_on_move_timeout,
            ),
            name="cryostat",
        )
    hold_temperature(cryostat.loop)
    set_mock_value(ls336.input_c.temp, 100.0)
    if error_on_move_timeout:
        with pytest.raises(TimeoutError):
            await cryostat.set(200.0, timeout=0.1)
    else:
        await cryostat.set(200.0, timeout=0.1)


async def test_cryostat_move(ls336: Lakeshore336, cryostat: Cryostat):
    set_mock_value(ls336.input_c.temp, 100.0)
    set_mock_value(cryostat.loop.ramp, 6.0)
    await cryostat.set(300.0)
    assert await cryostat.loop.sp.get_value() == 300.0
    assert (await cryostat.locate())["readback"] == 300.0


@pytest.mark.parametrize(
    "start, target, pid",
    [
        (10.0, 100.0, (50, 10, 1)),
        (100.0, 200.0, (25, 6, 3)),
        (100.0, 280.0, (26, 5, 3)),
        (100.0, 300.0, (26, 5, 3)),
        (300.0, 100.0, (25, 4, 3)),
        (300.0, 150.0, (25, 6, 3)),
        (300.0, 250.0, (35, 8, 3)),
    ],
)
async def test_cryostat_pid_schedule(
    ls336: Lakeshore336, cryostat: Cryostat, start, target, pid
):
    set_mock_value(ls336.input_c.temp, start)
    # Explicit timeout skips calculate_timeout, PIDs must still be applied.
    await cryostat.set(target, timeout=5)
    loop = cryostat.loop
    pids = await asyncio.gather(
        loop.p.get_value(), loop.i.get_value(), loop.d.get_value()
    )
    assert tuple(pids) == pid


async def test_cryostat_pids_unchanged_within_deadband(
    ls336: Lakeshore336, cryostat: Cryostat
):
    set_mock_value(ls336.input_c.temp, 100.0)
    await cryostat.set(100.4, timeout=5)
    get_mock_put(cryostat.loop.p).assert_not_called()


@pytest.mark.parametrize("target", [4.9, 550.1])
async def test_cryostat_limits(cryostat: Cryostat, target):
    with pytest.raises(ValueError, match="outside the cryostat limits"):
        await cryostat.set(target)
    get_mock_put(cryostat.loop.sp).assert_not_called()


async def test_cryostat_custom_limits(ls336: Lakeshore336):
    async with init_devices(mock=True):
        cryostat = Cryostat(ls336.create_loop(2, ls336.input_a), max_temp=300)
    with pytest.raises(ValueError):
        await cryostat.check_value(301)


async def test_cryostat_timeout(cryostat: Cryostat):
    set_mock_value(cryostat.loop.ramp, 6.0)  # K/min
    set_mock_value(cryostat.loop.sp, 100.0)
    # 60 K at 6 K/min = 600 s, plus the loop's settle_time (0.1 s) and 30 s buffer.
    assert await cryostat.movable_logic.calculate_timeout(100, 160) == pytest.approx(
        630.1
    )


async def test_cryostat_timeout_uses_farthest_of_readback_and_setpoint(
    cryostat: Cryostat,
):
    set_mock_value(cryostat.loop.ramp, 6.0)
    set_mock_value(cryostat.loop.sp, 400.0)  # setpoint far from the target
    # 240 K from the setpoint at 6 K/min = 2400 s, plus 0.1 s settle and 30 s buffer.
    assert await cryostat.movable_logic.calculate_timeout(100, 160) == pytest.approx(
        2430.1
    )


async def test_cryostat_uses_loop_settle_and_tolerance(ls336: Lakeshore336):
    async with init_devices(mock=True):
        cryostat = Cryostat(
            ls336.create_loop(2, ls336.input_a, tolerance=0.3, settle_time=7.0),
            name="cryostat",
        )
    logic = cryostat.movable_logic
    assert logic.settle_time is cryostat.loop.settle_time
    assert logic.in_tolerance is cryostat.loop.in_tolerance
    assert logic.target is cryostat.loop.target
    assert await cryostat.loop.tolerance.get_value() == 0.3
    set_mock_value(cryostat.loop.ramp, 6.0)
    set_mock_value(cryostat.loop.sp, 100.0)
    # 60 K at 6 K/min = 600 s, plus settle_time (7 s) and 30 s buffer.
    assert await logic.calculate_timeout(100, 160) == pytest.approx(637.0)


def test_cryostat_rejects_bad_pid_table():
    ls336 = Lakeshore336("LS:")
    loop = ls336.create_loop(1, ls336.input_a)
    with pytest.raises(ValueError, match="one more PID than thresholds"):
        Cryostat(loop, heating_pids=((100, 200), ((1, 2, 3), (4, 5, 6))))


async def test_cryostat_configure(ls336: Lakeshore336, cryostat: Cryostat):
    set_mock_value(ls336.input_c.temp, 42.0)
    RE = RunEngine({})
    RE(configure_cryostat(cryostat))
    loop = cryostat.loop
    assert await loop.sp.get_value() == 42.0
    assert await loop.loop_in_sel.get_value() == Lakeshore336LoopInput.C
    assert await loop.ramp_enbl.get_value() == Lakeshore336Switch.ON
    assert await loop.ramp.get_value() == 6.0
    assert await loop.enbl.get_value() == Lakeshore336Switch.ON
    assert await loop.loop_mode.get_value() == Lakeshore336LoopMode.PID
    assert await loop.maxi.get_value() == 2.0
    assert await loop.resistance.get_value() == Lakeshore336Resistance.LO25
    assert await loop.range.get_value() == Lakeshore336RangeSelect.RANGE3


async def test_loop_reading_is_sensor_temperature(ls336: Lakeshore336):
    async with init_devices(mock=True):
        loop = ls336.create_loop(3, ls336.input_b, name="loop")
    set_mock_value(ls336.input_b.temp, 55.0)
    set_mock_value(loop.sprdk, 99.0)

    reading = await loop.read()
    assert reading["loop"]["value"] == 55.0
    assert loop.hints == {"fields": ["loop"]}
    assert "loop-sprdk" in await loop.read_configuration()

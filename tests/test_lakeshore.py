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
from pdftools.plans import t_list


@pytest.fixture
async def ls336() -> Lakeshore336:
    async with init_devices(mock=True):
        ls336 = Lakeshore336("Test{LS336:1", name="ls336")
    return ls336


@pytest.fixture
async def cryostat(ls336: Lakeshore336) -> Cryostat:
    async with init_devices(mock=True):
        cryostat = Cryostat(
            ls336.loop(1, ls336.input_c), settle_time=0.1, name="cryostat"
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
    ls336.loop(1, ls336.input_c)
    with pytest.raises(ValueError, match="already been built"):
        ls336.loop(1, ls336.input_a)
    # Other loops are still available.
    ls336.loop(2, ls336.input_a)


async def test_loop_number_and_input_are_validated(ls336: Lakeshore336):
    with pytest.raises(ValueError, match="Loop number"):
        ls336.loop(5, ls336.input_a)
    other = Lakeshore336("OTHER:", name="other")
    with pytest.raises(ValueError, match="is not an input of ls336"):
        ls336.loop(1, other.input_a)


async def test_loop_prefix(ls336: Lakeshore336):
    loop = ls336.loop(3, ls336.input_a)
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
    assert set(reading) == {"cryostat", "cryostat-loop-sp", "cryostat-loop-sprdk"}
    assert cryostat.hints == {"fields": ["cryostat"]}

    config = await cryostat.read_configuration()
    assert set(config) == {
        "cryostat-tolerance",
        "cryostat-settle_time",
        "cryostat-loop-p",
        "cryostat-loop-i",
        "cryostat-loop-d",
        "cryostat-loop-ramp",
        "cryostat-loop-range",
        "cryostat-loop-loop_in_rb",
    }


async def test_loop_settles_within_tolerance(ls336: Lakeshore336):
    async with init_devices(mock=True):
        loop = ls336.loop(2, ls336.input_a, tolerance=0.5, settle_time=0.2)
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


async def test_loop_times_out(ls336: Lakeshore336):
    async with init_devices(mock=True):
        loop = ls336.loop(2, ls336.input_a, move_timeout=0.1)
    hold_temperature(loop)
    with pytest.raises(TimeoutError):
        await loop.set(20.0)


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
        cryostat = Cryostat(ls336.loop(2, ls336.input_a), max_temp=300)
    with pytest.raises(ValueError):
        await cryostat.check_value(301)


async def test_cryostat_timeout(cryostat: Cryostat):
    set_mock_value(cryostat.loop.ramp, 6.0)  # K/min
    # 60 K at 6 K/min = 600 s, plus settle_time (0.1 s) and 30 s buffer.
    assert await cryostat.movable_logic.calculate_timeout(100, 160) == pytest.approx(
        630.1
    )


def test_cryostat_rejects_bad_pid_table():
    ls336 = Lakeshore336("LS:")
    loop = ls336.loop(1, ls336.input_a)
    with pytest.raises(ValueError, match="one more PID than thresholds"):
        Cryostat(loop, heating_pids=((100, 200), ((1, 2, 3), (4, 5, 6))))


async def test_cryostat_configure(ls336: Lakeshore336, cryostat: Cryostat):
    set_mock_value(ls336.input_c.temp, 42.0)
    await cryostat.configure()
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


def test_t_list_continues_past_timeouts(RE: RunEngine):
    with init_devices(mock=True):
        ls336 = Lakeshore336("LS:", name="ls336")
        loop = ls336.loop(2, ls336.input_a, move_timeout=0.1)
    hold_temperature(loop)
    with pytest.warns(RuntimeWarning, match="did not settle"):
        RE(t_list(loop, [10.0, 20.0]))
    assert get_mock_put(loop.sp).call_count == 2

import warnings
from collections.abc import Sequence

import bluesky.plan_stubs as bps
from bluesky.protocols import NamedMovable
from bluesky.utils import FailedStatus


def move_and_continue(device: NamedMovable, target):
    """Move `device` to `target`; if it fails to settle in time, warn and
    let the plan proceed to the next point instead of aborting.

    Bluesky's RunEngine never lets the original device-side exception (e.g.
    the `TimeoutError` from a move that didn't settle) reach the plan: any
    failed status is re-raised into the plan as `bluesky.utils.FailedStatus`,
    with the real cause attached as `.__cause__`. Only a `TimeoutError` cause
    is swallowed here; anything else (an out-of-range setpoint, a dropped
    connection, ...) re-raises so it isn't silently lost in a long sweep.
    """
    try:
        yield from bps.mv(device, target)
    except FailedStatus as e:
        if not isinstance(e.__cause__, TimeoutError):
            raise
        warnings.warn(
            f"{device.name} did not settle at {target} ({e.__cause__!r}); continuing",
            RuntimeWarning,
            stacklevel=2,
        )
        return e
    return None


def t_list(device: NamedMovable, tlist: Sequence[float]):
    """Move `device` through each temperature in `tlist`, continuing past any
    setpoint that does not settle in time."""
    failures = []
    for temp in tlist:
        exc = yield from move_and_continue(device, temp)
        if exc is not None:
            failures.append((temp, exc))

    if failures:
        print(f"{len(failures)}/{len(tlist)} setpoints did not settle:")
        for temp, exc in failures:
            print(f"  {temp}: {exc.__cause__!r}")

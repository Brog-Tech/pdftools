from collections.abc import Callable
from pathlib import Path

import bluesky.plans as bp
import h5py
import numpy as np
import pytest
from bluesky.run_engine import RunEngine
from bluesky_tiled_plugins import TiledWriter
from ophyd_async.core import (
    StaticFilenameProvider,
    StaticPathProvider,
    callback_on_mock_put,
    init_devices,
    set_mock_value,
)
from ophyd_async.epics.adcore import ADBaseDataType, ADWriterFactory

from pdftools.detectors import XSPIO, XSPBitDepth, XSPDetector


@pytest.fixture
def xsp_io(RE: RunEngine):
    with init_devices(mock=True):
        xsp = XSPIO(prefix="TEST:XSP", name="xsp_io")
    return xsp


@pytest.fixture
async def xsp_detector_factory():
    def _factory(write_path: Path) -> XSPDetector:
        return XSPDetector(
            "TEST:XSP",
            ADWriterFactory.hdf(
                StaticPathProvider(StaticFilenameProvider("scan"), write_path)
            ),
            name="xsp",
        )

    return _factory


@pytest.fixture
def xsp_detector(
    RE: RunEngine, xsp_detector_factory: Callable[[Path], XSPDetector], tmp_path: Path
):
    with init_devices(mock=True):
        xsp = xsp_detector_factory(tmp_path)

    set_mock_value(xsp.hdf.file_path_exists, True)
    return xsp


async def test_detector_full_stack(RE, xsp_detector_factory, tiled_client, monkeypatch):

    monkeypatch.setenv(
        "OPHYD_ASYNC_PRESERVE_DETECTOR_STATE", "YES"
    )  # Ensure detector config is preserved across stages
    tmp_path, c = tiled_client
    with init_devices(mock=True):
        xsp = xsp_detector_factory(tmp_path)
    tiled_writer = TiledWriter(c)
    docs_cache: dict[str, list] = {}

    RE.subscribe(tiled_writer)
    RE.subscribe(lambda name, doc: docs_cache.setdefault(name, []).append(doc))

    set_mock_value(xsp.hdf.file_path_exists, True)
    set_mock_value(xsp.driver.array_size_x, 4)
    set_mock_value(xsp.driver.array_size_y, 3)
    set_mock_value(xsp.driver.num_images, 5)
    set_mock_value(xsp.driver.bit_depth, XSPBitDepth.TWELVE_BIT)
    set_mock_value(xsp.driver.data_type, ADBaseDataType.UINT16)

    def _on_acquire(value, **kwargs):
        if value:
            with h5py.File(tmp_path / "scan.h5", "w") as f:
                f.create_dataset(
                    "/entry/data/data",
                    data=np.random.randint(0, 65536, size=(5, 3, 4), dtype=np.uint16),
                )
            set_mock_value(xsp.driver.array_counter, 5)
            set_mock_value(xsp.hdf.num_captured, 5)

    callback_on_mock_put(xsp.driver.acquire, _on_acquire)

    RE(bp.count([xsp]))

    assert "start" in docs_cache
    assert "descriptor" in docs_cache
    assert "event" in docs_cache
    assert "stream_resource" in docs_cache
    assert "stream_datum" in docs_cache
    assert "stop" in docs_cache

    assert len(docs_cache["start"]) == 1
    assert len(docs_cache["descriptor"]) == 1
    assert len(docs_cache["event"]) == 1
    assert len(docs_cache["stream_resource"]) == 1
    assert len(docs_cache["stream_datum"]) == 1
    assert len(docs_cache["stop"]) == 1

    assert "xsp" in docs_cache["descriptor"][0]["data_keys"]
    desc = docs_cache["descriptor"][0]["data_keys"]["xsp"]
    assert desc["shape"] == [5, 3, 4]
    assert desc["dtype"] == "array"
    assert desc["dtype_numpy"] == "<u2"
    assert desc["source"].endswith("scan.h5")

    sres = docs_cache["stream_resource"][0]
    assert sres["uri"] == f"file://localhost{tmp_path / 'scan.h5'}"
    assert sres["parameters"]["dataset"] == "/entry/data/data"
    assert sres["parameters"]["chunk_shape"] == (1, 3, 4)

    datum = docs_cache["stream_datum"][0]
    assert datum["stream_resource"] == sres["uid"]
    assert datum["indices"] == {"start": 0, "stop": 1}
    assert datum["seq_nums"] == {"start": 1, "stop": 2}

    # Check that the data we get from the stream is what we expect
    # (the random data we wrote to the file in _on_download)
    run = c.values().last()
    assert "primary" in run
    assert "xsp" in run["primary"]
    data = run["primary"]["xsp"].read()
    assert data.shape == (5, 3, 4)
    assert data.dtype == np.uint16

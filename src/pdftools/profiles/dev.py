"""PDF-D Bluesky profile for the 28-ID-1 beamline at NSLS-II."""

from bluesky.callbacks.best_effort import BestEffortCallback
from bluesky.run_engine import RunEngine, autoawait_in_bluesky_event_loop
from bluesky.utils import ProgressBarManager
from bluesky_tiled_plugins import TiledWriter
from ophyd_async.core import StaticFilenameProvider, StaticPathProvider, init_devices
from ophyd_async.epics.adcore import ADWriterFactory
from tiled.client import simple

from pdftools.detectors import XSPDetector
from pdftools.lakeshore import Cryostat, Lakeshore336

# Create our RunEngine instance
RE: RunEngine = RunEngine(
    {
        "data_session": "pass-123456",
        "cycle": (
            # f"{datetime.today().year}-{int(datetime.today().month / 4) + 1}"
            "2026-3"
        ),
    }
)

# Set some metadata that never changes.
RE.md["facility"] = "NSLS-II"
RE.md["group"] = "PDF"
RE.md["beamline_id"] = "28-ID-1"

# Setup progress bars
RE.waiting_hook = ProgressBarManager()  # type: ignore[assignment]

tiled_reading_client = tiled_writing_client = c = simple(
    "/tmp/PDF_tools", api_key="PDFtools", readable_storage="/tmp/PDF_tools"
)

# Setup writing to the database
tw = TiledWriter(tiled_writing_client)
RE.subscribe(tw)

# Setup BEC
bec = BestEffortCallback()
RE.subscribe(bec)

autoawait_in_bluesky_event_loop()

# path_provider = NSLS2PathProvider(RE.md, beamline_tla="PDF")
path = "/nsls2/data/pdf/scratch/lambda_testing"
path_provider = StaticPathProvider(StaticFilenameProvider("lambda_tmp"), path)

with init_devices():
    lake = Lakeshore336("XF:28ID1-ES{LS336:1", name="lake")
    cryostat = Cryostat(
        lake.create_loop(
            1, lake.input_c, tolerance=1.0, settle_time=10, error_on_move_timeout=False
        )
    )
    lambda1 = XSPDetector(
        "XF:28ID1-ES{Lambda-Det:1}", ADWriterFactory.tiff(path_provider), name="lambda1"
    )

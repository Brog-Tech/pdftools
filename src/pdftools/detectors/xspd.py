import asyncio
from collections.abc import Sequence
from typing import Annotated as A

from ophyd_async.core import (
    DetectorTriggerLogic,
    EnabledDisabled,
    SignalR,
    SignalRW,
    StandardReadable,
    StrictEnum,
)
from ophyd_async.core import (
    StandardReadableFormat as Format,
)
from ophyd_async.epics.adcore import (
    ADAcquireLogic,
    ADBaseIO,
    ADImageMode,
    ADWriterFactory,
    AreaDetector,
    NDPluginBaseIO,
    trigger_info_from_num_images,
)
from ophyd_async.epics.core import (
    EpicsDevice,
    PvSuffix,
    epics_signal_r,
    epics_signal_rw_rbv,
)


class XSPBitDepth(StrictEnum):
    """
    Enum for XSP bit depth settings
    """

    ONE_BIT = "1 bit"
    SIX_BIT = "6 bit"
    TWELVE_BIT = "12 bit"
    TWENTY_FOUR_BIT = "24 bit"


class XSPImageMode(StrictEnum):
    """
    Enum for XSP image mode settings
    """

    SINGLE = "Single"
    MULTIPLE = "Multiple"


class XSPTriggerMode(StrictEnum):
    """
    Enum for XSP trigger mode settings
    """

    SOFTWARE = "Software"
    EXTERNAL_FRAMES = "External Frames"
    EXTERNAL_SEQUENCE = "External Sequence"


class XSPCounterMode(StrictEnum):
    """Enum for XSP counter mode settings"""

    SINGLE = "Single"
    DUAL = "Dual"


class XSPCompressLevel(StrictEnum):
    """Enum for XSP compression level settings"""

    ZERO = "0"
    ONE = "1"
    TWO = "2"
    THREE = "3"
    FOUR = "4"
    FIVE = "5"
    SIX = "6"
    SEVEN = "7"
    EIGHT = "8"
    NINE = "9"


class XSPCompressor(StrictEnum):
    """Enum for XSP compressor settings"""

    NONE = "none"
    ZLIB = "zlib"
    BLOSC_BLOSCLZ = "blosc/blosclz"
    BLOSC_LZ4 = "blosc/lz4"
    BLOSC_LZ4HC = "blosc/lz4hc"
    BLOSC_SNAPPY = "blosc/snappy"
    BLOSC_ZLIB = "blosc/zlib"
    BLOSC_ZSTD = "blosc/zstd"


class XSPShuffleMode(StrictEnum):
    """Enum for XSP shuffle mode settings"""

    NONE = "None"
    BYTE = "Byte Shuffle"
    BIT = "Bit Shuffle"
    AUTO = "Auto"


class XSPROIRows(StrictEnum):
    """Enum for XSP ROI rows settings"""

    ONE = "1"
    TWO = "2"
    FOUR = "4"
    EIGHT = "8"
    SIXTEEN = "16"
    THIRTY_TWO = "32"
    SIXTY_FOUR = "64"
    ONE_TWENTY_EIGHT = "128"
    TWO_FIFTY_SIX = "256"


class XSPModule(EpicsDevice):
    board_temp: A[SignalR[float], PvSuffix("BoardTemp_RBV")]
    fpga_temp: A[SignalR[float], PvSuffix("FPGATemp_RBV")]
    humidity: A[SignalR[float], PvSuffix("Humidity_RBV")]
    humidity_temp: A[SignalR[float], PvSuffix("HumidityTemp_RBV")]
    num_chips: A[SignalR[int], PvSuffix("NumChips_RBV")]
    max_frames: A[SignalR[int], PvSuffix("MaxFrames_RBV")]
    num_subframes: A[SignalR[int], PvSuffix("NumSubFrames_RBV")]
    num_connectors: A[SignalR[int], PvSuffix("NumConnectors_RBV")]
    interpolation_enabled: A[
        SignalRW[EnabledDisabled], PvSuffix("InterpolationMode_RBV")
    ]
    compress_level: A[SignalRW[XSPCompressLevel], PvSuffix("CompressLevel_RBV")]
    compressor_type: A[SignalRW[str], PvSuffix("CompressorType_RBV")]
    flatfield_enabled: A[SignalRW[EnabledDisabled], PvSuffix("FlatfieldEnabled_RBV")]
    low_threshold_flatfield_ts: A[SignalR[str], PvSuffix("LowThreshFfDate_RBV")]
    high_threshold_flatfield_ts: A[SignalR[str], PvSuffix("HighThreshFfDate_RBV")]
    low_threshold_flatfield_author: A[SignalR[str], PvSuffix("LowThreshFfAuthor_RBV")]
    high_threshold_flatfield_author: A[SignalR[str], PvSuffix("HighThreshFfAuthor_RBV")]
    ram_allocated: A[SignalR[bool], PvSuffix("RAMAllocated_RBV")]
    fames_queued: A[SignalR[int], PvSuffix("FramesQueued_RBV")]
    pixel_mask_enabled: A[SignalRW[EnabledDisabled], PvSuffix("PixelMask_RBV")]

    supports_hv_ctrl: A[SignalR[bool], PvSuffix("Features_RBV.B0")]
    supports_1_6_bit: A[SignalR[bool], PvSuffix("Features_RBV.B1")]
    supports_medipix_dac_io: A[SignalR[bool], PvSuffix("Features_RBV.B2")]
    supports_extended_gating: A[SignalR[bool], PvSuffix("Features_RBV.B3")]
    supports_roi_readout: A[SignalR[bool], PvSuffix("Features_RBV.B4")]

    rotation_yaw: A[SignalRW[float], PvSuffix("RotationYaw_RBV")]
    rotation_pitch: A[SignalRW[float], PvSuffix("RotationPitch_RBV")]
    rotation_roll: A[SignalRW[float], PvSuffix("RotationRoll_RBV")]

    x_position: A[SignalRW[float], PvSuffix("PositionX_RBV")]
    y_position: A[SignalRW[float], PvSuffix("PositionY_RBV")]
    z_position: A[SignalRW[float], PvSuffix("PositionZ_RBV")]

    voltage_hv: A[SignalRW[float], PvSuffix("VoltageHV_RBV")]
    sensor_current: A[SignalRW[float], PvSuffix("SensorCurrent_RBV")]
    saturation_threshold: A[SignalRW[int], PvSuffix("SaturationThreshold_RBV")]


class XSPIO(StandardReadable, ADBaseIO):
    def __init__(self, prefix: str, name: str = "") -> None:
        with self.add_children_as_readables(Format.CONFIG_SIGNAL):
            self.bit_depth = epics_signal_rw_rbv(XSPBitDepth, prefix + "BitDepth")
            self.trigger_mode = epics_signal_rw_rbv(
                XSPTriggerMode, prefix + "TriggerMode"
            )
            self.api_version = epics_signal_r(str, prefix + "APIVersion_RBV")
            self.xspd_version = epics_signal_r(str, prefix + "XSPDVersion_RBV")
            self.num_modules = epics_signal_r(int, prefix + "NumModules_RBV")
            self.beam_energy = epics_signal_rw_rbv(float, prefix + "BeamEnergy")
            self.saturation_flag = epics_signal_rw_rbv(
                EnabledDisabled, prefix + "SaturationFlag"
            )
            self.charge_summing = epics_signal_rw_rbv(
                EnabledDisabled, prefix + "ChargeSumming"
            )
            self.flatfield_correction = epics_signal_rw_rbv(
                EnabledDisabled, prefix + "FlatFieldCorrection"
            )
            self.gating_mode = epics_signal_rw_rbv(
                EnabledDisabled, prefix + "GatingMode"
            )
            self.counter_mode = epics_signal_rw_rbv(
                XSPCounterMode, prefix + "CounterMode"
            )
            self.roi_rows = epics_signal_rw_rbv(XSPROIRows, prefix + "ROIRows")
            self.low_threshold = epics_signal_rw_rbv(float, prefix + "LowThreshold")
            self.high_threshold = epics_signal_rw_rbv(float, prefix + "HighThreshold")
            self.count_rate_correction = epics_signal_rw_rbv(
                EnabledDisabled, prefix + "CountrateCorrection"
            )
            self.compressor = epics_signal_r(XSPCompressor, prefix + "Compressor_RBV")
            self.sensor_material = epics_signal_r(str, prefix + "SensorMaterial_RBV")
            self.sensor_thickness = epics_signal_r(
                float, prefix + "SensorThickness_RBV"
            )

        super().__init__(prefix, name=name)


class XSPTriggerLogic(DetectorTriggerLogic):
    def __init__(self, driver: XSPIO):
        self.driver = driver

    def config_sigs(self) -> set[SignalR]:
        return {
            self.driver.sdk_version,
            self.driver.firmware_version,
            self.driver.ad_core_version,
            self.driver.driver_version,
            self.driver.manufacturer,
            self.driver.model,
        }

    async def prepare_internal(self, num: int, livetime: float, deadtime: float):
        image_mode = ADImageMode.MULTIPLE if num != 1 else ADImageMode.SINGLE
        coros = [
            self.driver.image_mode.set(image_mode),
            self.driver.num_images.set(num),
        ]
        if livetime:
            coros.append(self.driver.acquire_time.set(livetime))
            if deadtime:
                coros.append(self.driver.acquire_period.set(livetime + deadtime))
        await asyncio.gather(*coros)

    async def default_trigger_info(self):
        return await trigger_info_from_num_images(self.driver)


class XSPDetector(AreaDetector[XSPIO]):
    """Create an ADXSPD AreaDetector instance

    :param prefix: EPICS PV prefix for the detector
    :param writer_factories: Factories for file writer plugins and their data logics,
        e.g. ``ADWriterFactory.hdf(path_provider)``
    :param driver_suffix: Suffix for the driver PV, defaults to "cam1:"
    :param plugins: Additional areaDetector plugins to include
    :param config_sigs: Additional signals to include in configuration
    :param name: Name for the detector device
    """

    def __init__(
        self,
        prefix: str,
        *writer_factories: ADWriterFactory,
        driver_suffix="cam1:",
        plugins: dict[str, NDPluginBaseIO] | None = None,
        config_sigs: Sequence[SignalR] = (),
        name: str = "",
    ) -> None:
        driver = XSPIO(prefix + driver_suffix)
        super().__init__(
            driver,
            prefix,
            *writer_factories,
            acquire_logic=ADAcquireLogic(driver),
            trigger_logic=XSPTriggerLogic(driver),
            plugins=plugins,
            config_sigs=config_sigs,
            name=name,
        )

"""Raspberry Pi CSI camera adapter using the OS-provided Picamera2 package."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import io
import logging
import time
from typing import Any

from embodied_runtime.sensing.camera.base import CameraBackend, CameraFrame

DEFAULT_SIZE = (640, 480)
LOGGER = logging.getLogger(__name__)


def _integers(value: Any, length: int) -> bool:
    return (isinstance(value, (tuple, list)) and len(value) == length
            and all(type(part) is int for part in value))


def _rectangle(value: Any) -> bool:
    return (_integers(value, 4) and min(value[:2]) >= 0
            and min(value[2:]) > 0)


@dataclass(frozen=True)
class _SensorMode:
    size: tuple[int, int]
    bit_depth: int
    crop: tuple[int, int, int, int]

    @property
    def sensor(self) -> dict[str, Any]:
        return {"output_size": self.size, "bit_depth": self.bit_depth}


def _read_modes(camera: Any) -> list[_SensorMode]:
    # Enumeration reconfigures the camera: do this before our final configure/start.
    try:
        reported = camera.sensor_modes
    except (AttributeError, KeyError, NotImplementedError):
        return []
    if not isinstance(reported, (list, tuple)) or not reported:
        return []
    modes = []
    for mode in reported:
        if not isinstance(mode, Mapping):
            return []
        size, depth, crop = mode.get("size"), mode.get("bit_depth"), mode.get("crop_limits")
        if (not _integers(size, 2) or min(size) <= 0
                or type(depth) is not int or depth <= 0 or not _rectangle(crop)):
            # Partial data cannot establish which mode has the widest coverage.
            return []
        modes.append(_SensorMode(tuple(size), depth, tuple(crop)))
    return modes


def _usable_crop(crop: tuple, size: tuple[int, int]) -> tuple[float, ...]:
    """Largest centred rectangle with the output aspect ratio, in sensor pixels."""
    x, y, width, height = crop
    usable_width = min(width, height * size[0] / size[1])
    usable_height = min(height, width * size[1] / size[0])
    return (x + (width - usable_width) / 2, y + (height - usable_height) / 2,
            usable_width, usable_height)


def _covers(crop: tuple, reference: tuple) -> bool:
    # Allow small driver alignment/subsampling margins (e.g. OV5647 VGA).
    # Compare overlap on BOTH axes, so a wide but short crop cannot win by area.
    return all(
        min(crop[axis] + crop[axis + 2], reference[axis] + reference[axis + 2])
        - max(crop[axis], reference[axis]) >= reference[axis + 2] * 0.98
        for axis in (0, 1)
    )


def _effective_configuration(camera: Any) -> Mapping:
    try:
        configuration = camera.camera_configuration()
    except (AttributeError, NotImplementedError):
        return {}
    return configuration if isinstance(configuration, Mapping) else {}


def _crop_controls(camera: Any) -> tuple[Any, Any]:
    try:
        control = camera.camera_controls.get("ScalerCrop")
    except (AttributeError, KeyError, NotImplementedError):
        return None, None
    if not isinstance(control, (list, tuple)) or len(control) != 3:
        return None, None
    return (tuple(control[1]) if _rectangle(control[1]) else None,
            tuple(control[2]) if _rectangle(control[2]) else None)


class Picamera2UnavailableError(RuntimeError):
    """Picamera2 support is unavailable in the current Python environment."""


class Picamera2DeviceUnavailableError(Picamera2UnavailableError):
    """Picamera2 is installed, but a usable CSI camera could not be opened."""


def _load_picamera2() -> Any:
    try:
        from picamera2 import Picamera2
    except (ImportError, ModuleNotFoundError) as error:
        raise Picamera2UnavailableError(
            "Picamera2 is unavailable in this Python environment; install the "
            "Raspberry Pi OS python3-picamera2 package and ensure the environment "
            "can access system site packages"
        ) from error
    return Picamera2


class Picamera2CameraBackend(CameraBackend):
    def __init__(
        self,
        camera_factory: Callable[[], Any] | None = None,
        *,
        size: tuple[int, int] = DEFAULT_SIZE,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self._camera_factory = camera_factory
        self._size = size
        self._clock_ns = clock_ns
        self._camera: Any | None = None
        self._running = False
        self._capture_crop_checked = False
        self._expected_crop: tuple | None = None

    @property
    def identifier(self) -> str:
        return "picamera2"

    @property
    def is_physical(self) -> bool:
        return True

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        factory = self._camera_factory or _load_picamera2()
        camera = None
        try:
            camera = factory()
            self._configure(camera)
            camera.start()
        except Picamera2UnavailableError:
            raise
        except BaseException as error:
            if camera is not None:
                try:
                    camera.close()
                except BaseException:
                    pass
            raise Picamera2DeviceUnavailableError(
                "Picamera2 is installed, but no usable CSI camera could be opened: "
                f"{error}"
            ) from error
        self._camera = camera
        self._running = True

    def _configure(self, camera: Any) -> None:
        modes = _read_modes(camera)

        def automatic() -> Mapping:
            camera.configure(camera.create_still_configuration(main={"size": self._size}))
            return _effective_configuration(camera)

        effective = automatic()
        selected = None
        reason = "metadata_unavailable"
        sensor = effective.get("sensor")
        candidates = [mode for mode in modes if all(
            mode.size[axis] >= self._size[axis] for axis in (0, 1)
        )]
        sensor_supported = (isinstance(sensor, Mapping)
                            and _integers(sensor.get("output_size"), 2)
                            and type(sensor.get("bit_depth")) is int
                            and sensor["bit_depth"] > 0)
        if candidates and sensor_supported:
            widest = max(candidates, key=lambda mode: (
                _usable_crop(mode.crop, self._size)[2]
                * _usable_crop(mode.crop, self._size)[3]
            ))
            reference = _usable_crop(widest.crop, self._size)
            equivalent = [mode for mode in candidates
                          if _covers(_usable_crop(mode.crop, self._size), reference)]
            selected = min(equivalent, key=lambda mode: (
                mode.size[0] * mode.size[1], -mode.bit_depth, mode.size
            ))
            current = next((mode for mode in modes if mode.sensor == sensor), None)
            maximum, _ = _crop_controls(camera)
            if (current is not None
                    and current.size[0] * current.size[1] <= selected.size[0] * selected.size[1]
                    and _covers(_usable_crop(current.crop, self._size), reference)
                    and (maximum is None or _covers(_usable_crop(maximum, self._size), reference))):
                selected = None
                reason = "automatic_coverage_sufficient"
            else:
                # The sensor field is supported by the verified Picamera2 versions;
                # older configurations without it stay on the automatic path.
                camera.configure(camera.create_still_configuration(
                    main={"size": self._size}, sensor=selected.sensor,
                ))
                effective = _effective_configuration(camera)
                maximum, _ = _crop_controls(camera)
                if (effective.get("sensor") != selected.sensor
                        or (maximum is not None and not _covers(
                            _usable_crop(maximum, self._size), reference))):
                    effective = automatic()
                    selected = None
                    reason = "effective_mode_unverified"
                else:
                    reason = "widest_economical_mode"
        elif modes:
            reason = "sensor_configuration_unsupported"

        main = effective.get("main")
        if isinstance(main, Mapping) and main.get("size") != self._size:
            raise RuntimeError(f"Camera changed requested output size: {main.get('size')}")
        maximum, default_crop = _crop_controls(camera)
        reported_mode = next((mode for mode in modes
                              if mode.sensor == effective.get("sensor")), None)
        reported_crop = reported_mode.crop if reported_mode else None
        self._expected_crop = maximum or reported_crop
        self._capture_crop_checked = False
        LOGGER.info(
            "[CAMERA] sensor=%s requested_sensor=%s output=%s reported_crop=%s crop_limits=%s "
            "scaler_crop_default=%s automatic_fallback=%s reason=%s",
            effective.get("sensor", "unknown"), selected.sensor if selected else "automatic",
            self._size, reported_crop, maximum, default_crop,
            selected is None and reason != "automatic_coverage_sufficient", reason,
        )

    def stop(self) -> None:
        camera = self._camera
        self._camera = None
        self._running = False
        if camera is None:
            return
        failure: BaseException | None = None
        try:
            camera.stop()
        except BaseException as error:
            failure = error
        try:
            camera.close()
        except BaseException as error:
            failure = failure or error
        if failure is not None:
            raise failure

    def capture_frame(self) -> CameraFrame:
        if not self._running or self._camera is None:
            raise RuntimeError("Camera capture requires a running backend")
        output = io.BytesIO()
        metadata = self._camera.capture_file(output, format="jpeg")
        captured_at_ns = self._clock_ns()
        if not self._capture_crop_checked:
            crop = metadata.get("ScalerCrop") if isinstance(metadata, Mapping) else None
            coverage = "unknown"
            if _rectangle(crop) and self._expected_crop is not None:
                coverage = "ok" if _covers(
                    _usable_crop(crop, self._size),
                    _usable_crop(self._expected_crop, self._size),
                ) else "reduced"
            log = LOGGER.warning if coverage == "reduced" else LOGGER.info
            log("[CAMERA] capture scaler_crop=%s coverage=%s", crop, coverage)
            self._capture_crop_checked = True
        return CameraFrame(
            data=output.getvalue(),
            media_type="image/jpeg",
            width=self._size[0],
            height=self._size[1],
            captured_at_ns=captured_at_ns,
        )

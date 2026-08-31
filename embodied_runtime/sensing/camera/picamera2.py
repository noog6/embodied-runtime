"""Raspberry Pi CSI camera adapter using the OS-provided Picamera2 package."""

from collections.abc import Callable
import io
import time
from typing import Any

from embodied_runtime.sensing.camera.base import CameraBackend, CameraFrame

DEFAULT_SIZE = (640, 480)


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
            configuration = camera.create_still_configuration(
                main={"size": self._size}
            )
            camera.configure(configuration)
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
        self._camera.capture_file(output, format="jpeg")
        return CameraFrame(
            data=output.getvalue(),
            media_type="image/jpeg",
            width=self._size[0],
            height=self._size[1],
            captured_at_ns=self._clock_ns(),
        )

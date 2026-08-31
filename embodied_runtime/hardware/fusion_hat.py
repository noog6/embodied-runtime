"""Minimal, sysfs-backed SunFounder Fusion HAT+ hardware foundation."""

from collections.abc import Callable
from pathlib import Path

from embodied_runtime.hardware.base import HardwareBackend


FUSION_HAT_SYSFS_ROOT = Path("/sys/class/fusion_hat/fusion_hat")
PWM_CHANNEL_COUNT = 12
SERVO_PERIOD_US = 20_000
SERVO_CENTER_PULSE_US = 1_500


class FusionHatUnavailableError(RuntimeError):
    """The explicitly selected Fusion HAT driver is not ready."""


def normalize_pwm_channel(channel: str | int) -> str:
    """Return the driver's canonical P0..P11 channel name."""
    if isinstance(channel, bool):
        raise ValueError("PWM channel must be P0 through P11")
    if isinstance(channel, int):
        number = channel
    elif (
        isinstance(channel, str)
        and channel.startswith("P")
        and channel[1:].isdigit()
        and channel[1:] == str(int(channel[1:]))
    ):
        number = int(channel[1:])
    else:
        raise ValueError("PWM channel must be P0 through P11")
    if not 0 <= number < PWM_CHANNEL_COUNT:
        raise ValueError("PWM channel must be P0 through P11")
    return f"P{number}"


class FusionHatSysfs:
    """The small portion of the official Fusion HAT sysfs ABI used here."""

    def __init__(
        self,
        root: str | Path = FUSION_HAT_SYSFS_ROOT,
        *,
        writer: Callable[[Path, str], None] | None = None,
    ) -> None:
        self.root = Path(root)
        self._writer = writer or self._write_text

    @staticmethod
    def _write_text(path: Path, value: str) -> None:
        path.write_text(value, encoding="ascii")

    @property
    def is_ready(self) -> bool:
        return self.root.is_dir()

    @property
    def pwm_root(self) -> Path:
        return self.root / "pwm"

    def pwm_channels(self) -> tuple[str, ...]:
        if not self.pwm_root.is_dir():
            return ()
        return tuple(
            f"P{number}" for number in range(PWM_CHANNEL_COUNT)
            if (self.pwm_root / f"pwm{number}").is_dir()
            and all(
                (self.pwm_root / f"pwm{number}" / item).is_file()
                for item in ("enable", "period", "duty_cycle")
            )
        )

    def open_pwm_channel(
        self,
        channel: str | int,
        on_close: Callable[["FusionHatPwmChannel"], None] | None = None,
    ) -> "FusionHatPwmChannel":
        name = normalize_pwm_channel(channel)
        number = int(name[1:])
        path = self.pwm_root / f"pwm{number}"
        # Derive the driver path only from the validated integer channel number.
        if name not in self.pwm_channels():
            raise RuntimeError(f"Fusion HAT PWM channel {name} is unavailable")
        return FusionHatPwmChannel(name, path, self._writer, on_close)


class FusionHatPwmChannel:
    """One low-level Fusion HAT PWM sysfs channel."""

    def __init__(
        self,
        name: str,
        path: Path,
        writer: Callable[[Path, str], None],
        on_close: Callable[["FusionHatPwmChannel"], None] | None = None,
    ) -> None:
        self.name = normalize_pwm_channel(name)
        self._path = path
        self._writer = writer
        self._on_close = on_close
        self._closed = False

    @property
    def is_closed(self) -> bool:
        return self._closed

    def _write(self, attribute: str, value: int) -> None:
        if self._closed:
            raise RuntimeError(f"Fusion HAT PWM channel {self.name} is closed")
        self._writer(self._path / attribute, str(value))

    def set_period_us(self, period_us: int) -> None:
        if type(period_us) is not int or period_us <= 0:
            raise ValueError("PWM period must be a positive integer in microseconds")
        self._write("period", period_us)

    def set_pulse_width_us(self, pulse_us: int) -> None:
        if type(pulse_us) is not int or pulse_us < 0:
            raise ValueError("PWM pulse width must be a non-negative integer in microseconds")
        self._write("duty_cycle", pulse_us)

    def enable(self) -> None:
        self._write("enable", 1)

    def disable(self) -> None:
        self._write("enable", 0)

    def close(self) -> None:
        if self._closed:
            return
        self.disable()
        self._closed = True
        if self._on_close is not None:
            self._on_close(self)


class FusionHatHardwareBackend(HardwareBackend):
    identifier = "fusion_hat"
    is_physical = True

    def __init__(self, sysfs: FusionHatSysfs | None = None) -> None:
        self.sysfs = sysfs or FusionHatSysfs()
        self._running = False
        self._capabilities: tuple[str, ...] = ()
        self._channels: set[FusionHatPwmChannel] = set()

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def capabilities(self) -> tuple[str, ...]:
        return self._capabilities

    @property
    def pwm_channels(self) -> tuple[str, ...]:
        return self.sysfs.pwm_channels() if self._running else ()

    def start(self) -> None:
        if self._running:
            return
        if not self.sysfs.is_ready:
            raise FusionHatUnavailableError(
                "Fusion HAT driver is not ready: expected "
                f"{self.sysfs.root}. Run `fusion_hat doctor` to diagnose "
                "the OS/driver installation."
            )
        channels = self.sysfs.pwm_channels()
        expected_channels = tuple(f"P{number}" for number in range(PWM_CHANNEL_COUNT))
        self._capabilities = ("pwm",) if channels == expected_channels else ()
        self._running = True

    def open_pwm_channel(self, channel: str | int) -> FusionHatPwmChannel:
        if not self._running:
            raise RuntimeError("Fusion HAT backend must be running before opening PWM")
        if "pwm" not in self._capabilities:
            raise RuntimeError("Fusion HAT PWM capability is unavailable")
        opened = self.sysfs.open_pwm_channel(channel, self._channels.discard)
        self._channels.add(opened)
        return opened

    def stop(self) -> None:
        failures: list[BaseException] = []
        try:
            for channel in tuple(self._channels):
                try:
                    channel.close()
                except BaseException as error:
                    failures.append(error)
        finally:
            self._running = False
            self._capabilities = ()
        if failures:
            raise RuntimeError(
                f"Failed to disable {len(failures)} Fusion HAT PWM channel(s)"
            ) from failures[0]

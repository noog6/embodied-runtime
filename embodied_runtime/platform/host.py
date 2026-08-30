"""Defensive, dependency-free probes for the local host platform."""

from collections.abc import Iterable
import os
from pathlib import Path
import platform
import socket
import time

from embodied_runtime.platform.base import PlatformSnapshot


def parse_uptime(text: str) -> float | None:
    try:
        value = float(text.split()[0])
        return value if value >= 0 else None
    except (IndexError, ValueError):
        return None


def parse_meminfo(text: str) -> tuple[int | None, int | None]:
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, separator, raw = line.partition(":")
        if not separator:
            continue
        fields = raw.split()
        try:
            value = int(fields[0])
        except (IndexError, ValueError):
            continue
        multiplier = 1024 if len(fields) > 1 and fields[1].lower() == "kb" else 1
        values[key] = value * multiplier
    return values.get("MemTotal"), values.get("MemAvailable")


def parse_device_model(data: bytes) -> str | None:
    model = data.decode("utf-8", errors="replace").rstrip("\x00\r\n").strip()
    return model or None


def parse_temperature(text: str) -> float | None:
    try:
        value = float(text.strip())
    except ValueError:
        return None
    # Linux thermal zones conventionally expose millidegrees, while fixtures
    # and some platforms may expose degrees directly.
    if abs(value) >= 1000:
        value /= 1000
    return value if -273.15 <= value <= 1000 else None


class HostPlatformProvider:
    """Collect a current snapshot from portable APIs and optional Linux files."""

    _CPU_ZONE_TYPES = ("cpu", "soc", "package", "x86_pkg_temp", "bcm")

    def __init__(
        self,
        *,
        proc_root: Path = Path("/proc"),
        device_model_paths: Iterable[Path] | None = None,
        thermal_root: Path = Path("/sys/class/thermal"),
    ) -> None:
        self._proc_root = proc_root
        if device_model_paths is None:
            device_model_paths = (
                Path("/proc/device-tree/model"),
                Path("/sys/firmware/devicetree/base/model"),
            )
        self._device_model_paths = tuple(device_model_paths)
        self._thermal_root = thermal_root

    @staticmethod
    def _read_text(path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None

    def _model(self) -> str | None:
        for path in self._device_model_paths:
            try:
                model = parse_device_model(path.read_bytes())
            except OSError:
                continue
            if model:
                return model
        return None

    def _temperature(self) -> float | None:
        try:
            zones = tuple(self._thermal_root.glob("thermal_zone*"))
        except OSError:
            return None
        preferred: list[Path] = []
        fallback: list[Path] = []
        for zone in zones:
            zone_type = self._read_text(zone / "type")
            target = preferred if zone_type and any(
                marker in zone_type.strip().lower() for marker in self._CPU_ZONE_TYPES
            ) else fallback
            target.append(zone)
        for zone in (*preferred, *fallback):
            raw = self._read_text(zone / "temp")
            temperature = parse_temperature(raw) if raw is not None else None
            if temperature is not None:
                return temperature
        return None

    def snapshot(self) -> PlatformSnapshot:
        uptime_text = self._read_text(self._proc_root / "uptime")
        meminfo_text = self._read_text(self._proc_root / "meminfo")
        memory_total, memory_available = (
            parse_meminfo(meminfo_text) if meminfo_text is not None else (None, None)
        )
        try:
            load_averages = tuple(os.getloadavg())
        except (AttributeError, OSError):
            load_averages = None
        return PlatformSnapshot(
            hostname=socket.gethostname(),
            system=platform.system(),
            release=platform.release(),
            machine=platform.machine(),
            python_version=platform.python_version(),
            model=self._model(),
            uptime_seconds=parse_uptime(uptime_text) if uptime_text is not None else None,
            load_averages=load_averages,
            memory_total_bytes=memory_total,
            memory_available_bytes=memory_available,
            cpu_temperature_celsius=self._temperature(),
            captured_monotonic=time.monotonic(),
        )

from dataclasses import FrozenInstanceError
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from embodied_runtime.platform import HostPlatformProvider, PlatformSnapshot
from embodied_runtime.platform.host import (
    parse_device_model,
    parse_meminfo,
    parse_temperature,
    parse_uptime,
)


def snapshot(**overrides: object) -> PlatformSnapshot:
    values = dict(
        hostname="test-host",
        system="TestOS",
        release="1",
        machine="test64",
        python_version="3.13.5",
        model=None,
        uptime_seconds=None,
        load_averages=None,
        memory_total_bytes=None,
        memory_available_bytes=None,
        cpu_temperature_celsius=None,
        captured_monotonic=1.0,
    )
    values.update(overrides)
    return PlatformSnapshot(**values)  # type: ignore[arg-type]


class PlatformSnapshotTests(unittest.TestCase):
    def test_snapshot_is_immutable(self) -> None:
        state = snapshot()
        with self.assertRaises(FrozenInstanceError):
            state.hostname = "changed"  # type: ignore[misc]


class HostProbeTests(unittest.TestCase):
    def test_portable_identity_collection_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = HostPlatformProvider(
                proc_root=root,
                device_model_paths=(root / "missing",),
                thermal_root=root / "missing",
            ).snapshot()
        self.assertTrue(state.hostname)
        self.assertTrue(state.system)
        self.assertTrue(state.python_version)

    def test_uptime_parsing(self) -> None:
        self.assertEqual(parse_uptime("123.45 99.0\n"), 123.45)

    def test_meminfo_parsing(self) -> None:
        total, available = parse_meminfo(
            "MemTotal:       512000 kB\nMemFree: 1 kB\nMemAvailable: 350000 kB\n"
        )
        self.assertEqual(total, 512000 * 1024)
        self.assertEqual(available, 350000 * 1024)

    def test_device_model_strips_nul_and_newline(self) -> None:
        self.assertEqual(
            parse_device_model(b"Raspberry Pi Zero 2 W Rev 1.0\x00\n"),
            "Raspberry Pi Zero 2 W Rev 1.0",
        )

    def test_temperature_parsing(self) -> None:
        self.assertEqual(parse_temperature("42750\n"), 42.75)

    def test_cpu_zone_is_preferred(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gpu = root / "thermal_zone0"
            cpu = root / "thermal_zone1"
            gpu.mkdir()
            cpu.mkdir()
            (gpu / "type").write_text("gpu\n")
            (gpu / "temp").write_text("51000\n")
            (cpu / "type").write_text("cpu-thermal\n")
            (cpu / "temp").write_text("42750\n")
            state = HostPlatformProvider(
                proc_root=root / "missing", device_model_paths=(), thermal_root=root
            ).snapshot()
        self.assertEqual(state.cpu_temperature_celsius, 42.75)

    def test_missing_optional_telemetry_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "embodied_runtime.platform.host.os.getloadavg", side_effect=OSError
        ):
            root = Path(directory)
            state = HostPlatformProvider(
                proc_root=root / "missing",
                device_model_paths=(root / "missing",),
                thermal_root=root / "missing",
            ).snapshot()
        self.assertIsNone(state.model)
        self.assertIsNone(state.uptime_seconds)
        self.assertIsNone(state.load_averages)
        self.assertIsNone(state.memory_total_bytes)
        self.assertIsNone(state.memory_available_bytes)
        self.assertIsNone(state.cpu_temperature_celsius)

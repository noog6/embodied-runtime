from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from embodied_runtime.app import RobotApplication
from embodied_runtime.body.virtual import VirtualBodyBackend
from embodied_runtime.cli import run_fusion_battery_test, run_fusion_servo_test
from embodied_runtime.events import EventBus
from embodied_runtime.hardware.fusion_hat import (
    FUSION_HAT_SYSFS_ROOT,
    FusionHatHardwareBackend,
    FusionHatSysfs,
    normalize_pwm_channel,
)
from embodied_runtime.hardware.base import HardwareBackend
from embodied_runtime.profile import RobotProfile
from embodied_runtime.state import BodyState, LifecycleState
from tests.test_platform import snapshot


def fake_sysfs(root: Path, channels=range(12)) -> FusionHatSysfs:
    for number in channels:
        path = root / "pwm" / f"pwm{number}"
        path.mkdir(parents=True, exist_ok=True)
        for attribute in ("enable", "period", "duty_cycle"):
            (path / attribute).write_text("unchanged", encoding="ascii")
    return FusionHatSysfs(root)


def add_battery_adc(root: Path, raw: int) -> None:
    path = root / "adc" / "adc4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(raw), encoding="ascii")


class PlatformProvider:
    def snapshot(self):
        return snapshot()


class FusionHatSysfsTests(unittest.TestCase):
    def test_official_default_root(self):
        self.assertEqual(FusionHatSysfs().root, FUSION_HAT_SYSFS_ROOT)
        self.assertEqual(str(FUSION_HAT_SYSFS_ROOT), "/sys/class/fusion_hat/fusion_hat")

    def test_missing_and_ready_device_roots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "missing"
            self.assertFalse(FusionHatSysfs(root).is_ready)
            root.mkdir()
            self.assertTrue(FusionHatSysfs(root).is_ready)

    def test_pwm_requires_complete_interface(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sysfs = fake_sysfs(root)
            self.assertEqual(sysfs.pwm_channels(), tuple(f"P{n}" for n in range(12)))
            (root / "pwm" / "pwm11" / "enable").unlink()
            self.assertEqual(sysfs.pwm_channels(), tuple(f"P{n}" for n in range(11)))

    def test_channel_validation(self):
        for value, expected in (("P0", "P0"), ("P11", "P11"), (0, "P0"), (11, "P11")):
            with self.subTest(value=value):
                self.assertEqual(normalize_pwm_channel(value), expected)
        for value in (
            "P12", "P99", "P00", -1, 12, "-1", "nonsense", "../P0", True,
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_pwm_channel(value)

    def test_writes_are_scoped_and_close_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sysfs = fake_sysfs(root, channels=(0, 1))
            channel = sysfs.open_pwm_channel("P0")
            channel.set_period_us(20_000)
            channel.set_pulse_width_us(1_500)
            channel.enable()
            self.assertEqual((root / "pwm/pwm0/period").read_text(), "20000")
            self.assertEqual((root / "pwm/pwm0/duty_cycle").read_text(), "1500")
            self.assertEqual((root / "pwm/pwm0/enable").read_text(), "1")
            self.assertEqual((root / "pwm/pwm1/period").read_text(), "unchanged")
            self.assertFalse((root / "pwm/P0").exists())
            channel.disable()
            self.assertEqual((root / "pwm/pwm0/enable").read_text(), "0")
            channel.close()
            channel.close()

    def test_operator_channels_map_to_driver_directory_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sysfs = fake_sysfs(root)
            sysfs.open_pwm_channel("P0").set_period_us(101)
            sysfs.open_pwm_channel("P11").set_period_us(111)
            self.assertEqual((root / "pwm/pwm0/period").read_text(), "101")
            self.assertEqual((root / "pwm/pwm11/period").read_text(), "111")
            self.assertFalse((root / "pwm/P0").exists())
            self.assertFalse((root / "pwm/P11").exists())


class FusionHatBackendTests(unittest.TestCase):
    def test_battery_conversion_uses_fusion_hat_a4_divider(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            add_battery_adc(root, 3127)
            backend = FusionHatHardwareBackend(fake_sysfs(root))
            backend.start()
            reading = backend.read_battery_voltage()
            self.assertEqual(reading.adc_raw, 3127)
            self.assertAlmostEqual(reading.a4_voltage, 3127 / 4095.0 * 3.3)
            self.assertAlmostEqual(
                reading.battery_voltage, 3127 / 4095.0 * 3.3 * 3.0
            )
            self.assertEqual(backend.capabilities, ("pwm", "battery_voltage"))

    def test_battery_request_reads_a4_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            add_battery_adc(root, 2048)
            reads = []

            def reader(path):
                reads.append(path)
                return path.read_text(encoding="ascii")

            backend = FusionHatHardwareBackend(FusionHatSysfs(root, reader=reader))
            backend.start()
            backend.read_battery_voltage()
            self.assertEqual(reads, [root / "adc" / "adc4"])

    def test_battery_read_requires_running_available_a4(self):
        with tempfile.TemporaryDirectory() as temporary:
            backend = FusionHatHardwareBackend(fake_sysfs(Path(temporary)))
            with self.assertRaisesRegex(RuntimeError, "must be running"):
                backend.read_battery_voltage()
            backend.start()
            with self.assertRaisesRegex(RuntimeError, "capability is unavailable"):
                backend.read_battery_voltage()

    def test_metadata_lifecycle_and_discovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            backend = FusionHatHardwareBackend(fake_sysfs(Path(temporary)))
            self.assertEqual(backend.identifier, "fusion_hat")
            self.assertIsInstance(backend, HardwareBackend)
            self.assertTrue(backend.is_physical)
            self.assertFalse(backend.is_running)
            backend.start()
            self.assertTrue(backend.is_running)
            self.assertEqual(backend.capabilities, ("pwm",))
            backend.stop()
            backend.stop()
            self.assertFalse(backend.is_running)

    def test_ready_board_without_pwm_reports_no_capability(self):
        with tempfile.TemporaryDirectory() as temporary:
            backend = FusionHatHardwareBackend(FusionHatSysfs(temporary))
            backend.start()
            self.assertEqual(backend.capabilities, ())
            backend.stop()

    def test_partial_pwm_tree_does_not_advertise_capability(self):
        with tempfile.TemporaryDirectory() as temporary:
            backend = FusionHatHardwareBackend(
                fake_sysfs(Path(temporary), channels=range(11))
            )
            backend.start()
            self.assertEqual(backend.pwm_channels, tuple(f"P{n}" for n in range(11)))
            self.assertEqual(backend.capabilities, ())
            with self.assertRaisesRegex(RuntimeError, "capability is unavailable"):
                backend.open_pwm_channel("P0")
            backend.stop()

    def test_start_performs_no_physical_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_sysfs(root)
            writes = []
            backend = FusionHatHardwareBackend(
                FusionHatSysfs(
                    root, writer=lambda path, value: writes.append((path, value))
                )
            )
            backend.start()
            self.assertEqual(backend.capabilities, ("pwm",))
            self.assertEqual(writes, [])
            backend.stop()

    def test_missing_driver_is_operator_useful_and_never_running(self):
        with tempfile.TemporaryDirectory() as temporary:
            backend = FusionHatHardwareBackend(FusionHatSysfs(Path(temporary) / "missing"))
            with self.assertRaisesRegex(RuntimeError, "fusion_hat doctor"):
                backend.start()
            self.assertFalse(backend.is_running)

    def test_open_requires_running_and_stop_disables_owned_channels(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backend = FusionHatHardwareBackend(fake_sysfs(root))
            with self.assertRaisesRegex(RuntimeError, "running"):
                backend.open_pwm_channel("P0")
            backend.start()
            channel = backend.open_pwm_channel("P0")
            channel.enable()
            self.assertEqual(len(backend._channels), 1)
            backend.stop()
            self.assertEqual((root / "pwm/pwm0/enable").read_text(), "0")
            self.assertTrue(channel.is_closed)

    def test_cleanup_failure_does_not_skip_other_channels(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_sysfs(root)
            failed_once = False

            def writer(path, value):
                nonlocal failed_once
                if (
                    path.parent.name == "pwm0"
                    and path.name == "enable"
                    and value == "0"
                    and not failed_once
                ):
                    failed_once = True
                    raise OSError("first disable failed")
                path.write_text(value, encoding="ascii")

            backend = FusionHatHardwareBackend(FusionHatSysfs(root, writer=writer))
            backend.start()
            first = backend.open_pwm_channel("P0")
            second = backend.open_pwm_channel("P1")
            first.enable()
            second.enable()
            with self.assertRaisesRegex(RuntimeError, "Failed to disable"):
                backend.stop()
            self.assertEqual((root / "pwm/pwm1/enable").read_text(), "0")
            self.assertTrue(second.is_closed)
            self.assertNotIn(second, backend._channels)
            self.assertFalse(first.is_closed)
            self.assertIn(first, backend._channels)
            self.assertFalse(backend.is_running)
            backend.stop()
            self.assertEqual((root / "pwm/pwm0/enable").read_text(), "0")
            self.assertTrue(first.is_closed)
            self.assertNotIn(first, backend._channels)


class ServoDiagnosticTests(unittest.TestCase):
    def recording_backend(self, root, operations, *, fail_attribute=None):
        enabled = {}

        def writer(path, value):
            attribute = path.name
            operations.append((path.parent.name, attribute, value))
            if attribute == "duty_cycle" and not enabled.get(path.parent.name, False):
                raise AssertionError("driver rejects duty_cycle while disabled")
            if attribute == fail_attribute:
                raise OSError(f"{attribute} write failed")
            path.write_text(value, encoding="ascii")
            if attribute == "enable":
                enabled[path.parent.name] = value == "1"

        sysfs = fake_sysfs(root)
        sysfs._writer = writer
        backend = FusionHatHardwareBackend(sysfs)
        backend.start()
        return backend

    def test_explicit_test_uses_driver_required_write_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            operations = []
            backend = self.recording_backend(root, operations)

            def hold(seconds):
                operations.append(("hold", seconds))

            result = run_fusion_servo_test(
                backend, "P0", sleeper=hold
            )
            self.assertEqual(
                operations,
                [
                    ("pwm0", "enable", "0"),
                    ("pwm0", "enable", "1"),
                    ("pwm0", "period", "20000"),
                    ("pwm0", "duty_cycle", "1500"),
                    ("hold", 0.5),
                    ("pwm0", "enable", "0"),
                ],
            )
            self.assertEqual((root / "pwm/pwm0/enable").read_text(), "0")
            self.assertEqual((root / "pwm/pwm1/enable").read_text(), "unchanged")
            self.assertIn("channel=P0 pulse_us=1500 period_us=20000 status=ok", result)
            backend.stop()

    def test_sleep_failure_still_disables_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            operations = []
            backend = self.recording_backend(root, operations)
            def fail(_seconds):
                raise RuntimeError("interrupted")
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                run_fusion_servo_test(backend, "P0", sleeper=fail)
            self.assertEqual(operations[-1], ("pwm0", "enable", "0"))
            self.assertEqual((root / "pwm/pwm0/enable").read_text(), "0")
            backend.stop()

    def test_period_or_duty_failure_still_disables_output(self):
        for attribute in ("period", "duty_cycle"):
            with self.subTest(attribute=attribute), tempfile.TemporaryDirectory() as temporary:
                operations = []
                backend = self.recording_backend(
                    Path(temporary), operations, fail_attribute=attribute
                )
                with self.assertRaisesRegex(OSError, f"{attribute} write failed"):
                    run_fusion_servo_test(backend, "P0", sleeper=lambda _: None)
                self.assertEqual(operations[-1], ("pwm0", "enable", "0"))
                backend.stop()

    def test_reporting_failure_occurs_only_after_output_is_disabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            operations = []
            backend = self.recording_backend(root, operations)
            with patch("builtins.print", side_effect=RuntimeError("report failed")):
                with self.assertRaisesRegex(RuntimeError, "report failed"):
                    print(run_fusion_servo_test(backend, "P0", sleeper=lambda _: None))
            self.assertEqual(operations[-1], ("pwm0", "enable", "0"))
            self.assertEqual((root / "pwm/pwm0/enable").read_text(), "0")
            backend.stop()


class BatteryDiagnosticTests(unittest.TestCase):
    def test_reports_raw_adc_and_both_voltages(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            add_battery_adc(root, 3127)
            backend = FusionHatHardwareBackend(fake_sysfs(root))
            backend.start()
            self.assertEqual(
                run_fusion_battery_test(backend),
                "[BATTERY] adc_raw=3127 adc_v=2.520 battery_v=7.560",
            )


class PhysicalCompositionTests(unittest.IsolatedAsyncioTestCase):
    async def test_physical_hardware_coexists_with_virtual_body(self):
        with tempfile.TemporaryDirectory() as temporary:
            hardware = FusionHatHardwareBackend(fake_sysfs(Path(temporary)))
            app = RobotApplication(
                RobotProfile("test", "Test"), hardware,
                platform_provider=PlatformProvider(), body_backend=VirtualBodyBackend(),
            )
            await app.start()
            self.assertTrue(app.summary().hardware_is_physical)
            self.assertFalse(app.body_summary().is_physical)
            self.assertEqual(app.runtime_state.body, BodyState(0.0, 0.0))
            await app.stop()

    async def test_missing_driver_start_failure_stops_everything(self):
        with tempfile.TemporaryDirectory() as temporary:
            events = EventBus()
            hardware = FusionHatHardwareBackend(FusionHatSysfs(Path(temporary) / "missing"))
            body = VirtualBodyBackend()
            app = RobotApplication(
                RobotProfile("test", "Test"), hardware, events=events,
                platform_provider=PlatformProvider(), body_backend=body,
            )
            with self.assertRaisesRegex(RuntimeError, "fusion_hat doctor"):
                await app.start()
            self.assertEqual(app.state, LifecycleState.STOPPED)
            self.assertFalse(events.is_running)
            self.assertFalse(hardware.is_running)
            self.assertFalse(body._running)

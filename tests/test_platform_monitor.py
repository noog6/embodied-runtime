import asyncio
from dataclasses import FrozenInstanceError
import unittest

from embodied_runtime.app import RobotApplication
from embodied_runtime.events import (
    Event,
    EventBus,
    MemoryPressureCleared,
    MemoryPressureRaised,
    ThermalWarningCleared,
    ThermalWarningRaised,
)
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.platform import PlatformMonitor, PlatformMonitorPolicy
from embodied_runtime.profile import RobotProfile
from tests.test_platform import snapshot


class Provider:
    def __init__(self, values):
        self.values = iter(values)
        self.calls = 0

    def snapshot(self):
        self.calls += 1
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        return value


class PolicyTests(unittest.TestCase):
    def test_defaults_and_immutability(self):
        policy = PlatformMonitorPolicy()
        self.assertEqual(
            (
                policy.interval_seconds,
                policy.thermal_warning_celsius,
                policy.thermal_clear_celsius,
                policy.memory_pressure_ratio,
                policy.memory_clear_ratio,
            ),
            (5.0, 80.0, 75.0, 0.10, 0.15),
        )
        with self.assertRaises(FrozenInstanceError):
            policy.interval_seconds = 1  # type: ignore[misc]

    def test_rejects_invalid_configuration(self):
        invalid = [
            dict(interval_seconds=0),
            dict(interval_seconds=float("nan")),
            dict(thermal_warning_celsius=75, thermal_clear_celsius=75),
            dict(memory_pressure_ratio=0.2, memory_clear_ratio=0.1),
            dict(memory_pressure_ratio=-0.1),
            dict(memory_clear_ratio=1.1),
        ]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                PlatformMonitorPolicy(**values)


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bus = EventBus()
        self.events: list[Event] = []

        async def record(event: Event):
            self.events.append(event)

        self.bus.subscribe(Event, record)
        await self.bus.start()
        self.current = snapshot(
            cpu_temperature_celsius=70,
            memory_total_bytes=1000,
            memory_available_bytes=500,
        )

    async def asyncTearDown(self):
        await self.bus.stop()

    def monitor(self, samples):
        monitor = PlatformMonitor(
            Provider(samples),
            self.bus,
            lambda value: setattr(self, "current", value),
            lambda: True,
        )
        monitor.establish_baseline(self.current)
        return monitor

    async def cycle(self, monitor):
        await monitor.sample_platform_once()
        await asyncio.sleep(0)

    async def test_thermal_hysteresis_missing_and_no_duplicates(self):
        values = [70, 80, 83, None, 77, 75]
        monitor = self.monitor(
            [snapshot(cpu_temperature_celsius=value) for value in values]
        )
        for expected_count in [0, 1, 1, 1, 1, 2]:
            await self.cycle(monitor)
            self.assertEqual(len(self.events), expected_count)
        self.assertEqual(
            [type(event) for event in self.events],
            [ThermalWarningRaised, ThermalWarningCleared],
        )
        raised = self.events[0]
        self.assertEqual(raised.source, "platform_monitor")
        self.assertEqual(raised.warning_threshold_celsius, 80)  # type: ignore[attr-defined]

    async def test_memory_hysteresis_invalid_and_no_duplicates(self):
        samples = [
            snapshot(memory_total_bytes=1000, memory_available_bytes=100),
            snapshot(memory_total_bytes=1000, memory_available_bytes=50),
            snapshot(memory_total_bytes=0, memory_available_bytes=0),
            snapshot(memory_total_bytes=1000, memory_available_bytes=120),
            snapshot(memory_total_bytes=1000, memory_available_bytes=150),
        ]
        monitor = self.monitor(samples)
        for expected_count in [1, 1, 1, 1, 2]:
            await self.cycle(monitor)
            self.assertEqual(len(self.events), expected_count)
        self.assertEqual(
            [type(event) for event in self.events],
            [MemoryPressureRaised, MemoryPressureCleared],
        )
        raised = self.events[0]
        self.assertEqual(raised.available_ratio, 0.1)  # type: ignore[attr-defined]
        self.assertEqual(raised.memory_available_bytes, 100)  # type: ignore[attr-defined]

    async def test_unavailable_baseline_later_warning_raises(self):
        self.current = snapshot()
        monitor = self.monitor(
            [
                snapshot(
                    cpu_temperature_celsius=81,
                    memory_total_bytes=100,
                    memory_available_bytes=5,
                )
            ]
        )
        await self.cycle(monitor)
        self.assertEqual(
            [type(event) for event in self.events],
            [ThermalWarningRaised, MemoryPressureRaised],
        )

    async def test_state_replaced_before_handler_observes_transition(self):
        observed = []
        replacement = snapshot(cpu_temperature_celsius=81, captured_monotonic=2)

        async def inspect(event: ThermalWarningRaised):
            observed.append(self.current)

        self.bus.subscribe(ThermalWarningRaised, inspect)
        monitor = self.monitor([replacement])
        old = self.current
        await self.cycle(monitor)
        self.assertIs(self.current, replacement)
        self.assertIsNot(self.current, old)
        self.assertEqual(observed, [replacement])

    async def test_provider_exception_preserves_state(self):
        monitor = self.monitor([RuntimeError("probe failed")])
        old = self.current
        with self.assertRaisesRegex(RuntimeError, "probe failed"):
            await monitor.sample_platform_once()
        self.assertIs(self.current, old)


class ApplicationMonitorTests(unittest.IsolatedAsyncioTestCase):
    def app(self, provider, *, bus=None, interval=0.01):
        return RobotApplication(
            RobotProfile("test", "Test"),
            VirtualHardwareBackend(),
            events=bus,
            platform_provider=provider,
            platform_monitor_policy=PlatformMonitorPolicy(interval_seconds=interval),
        )

    async def test_monitor_starts_only_after_successful_start_and_stops(self):
        provider = Provider([snapshot()] * 3)
        app = self.app(provider)
        self.assertFalse(app._platform_monitor.is_running)
        await app.start()
        self.assertTrue(app._platform_monitor.is_running)
        await app.stop()
        self.assertFalse(app._platform_monitor.is_running)
        calls = provider.calls
        await asyncio.sleep(0.02)
        self.assertEqual(provider.calls, calls)

    async def test_hardware_failure_leaves_no_monitor(self):
        hardware = VirtualHardwareBackend()

        def fail():
            raise RuntimeError("failed")

        hardware.start = fail  # type: ignore[method-assign]
        app = RobotApplication(
            RobotProfile("test", "Test"),
            hardware,
            platform_provider=Provider([snapshot()]),
        )
        with self.assertRaises(RuntimeError):
            await app.start()
        self.assertFalse(app._platform_monitor.is_running)

    async def test_exception_does_not_kill_background_monitor(self):
        raised = asyncio.Event()
        bus = EventBus()

        async def receive(event):
            raised.set()

        bus.subscribe(ThermalWarningRaised, receive)
        provider = Provider(
            [
                snapshot(cpu_temperature_celsius=70),
                RuntimeError("once"),
                snapshot(cpu_temperature_celsius=81),
                snapshot(),
            ]
        )
        app = self.app(provider, bus=bus, interval=0.001)
        with self.assertLogs("embodied_runtime.platform.monitor", level="ERROR"):
            await app.start()
            await asyncio.wait_for(raised.wait(), 1)
        self.assertTrue(app._platform_monitor.is_running)
        await app.stop()

    async def test_backpressured_publish_is_cancelled_by_stop(self):
        bus = EventBus(queue_size=1)
        started, release = asyncio.Event(), asyncio.Event()

        async def blocked(event):
            started.set()
            await release.wait()

        subscription = bus.subscribe(ThermalWarningRaised, blocked)
        provider = Provider(
            [snapshot(cpu_temperature_celsius=70)]
            + [snapshot(cpu_temperature_celsius=81)] * 10
        )
        app = self.app(provider, bus=bus, interval=0.1)
        await app.start()
        # Occupy the worker and queue before the monitor's transition publish.
        await bus.publish(
            ThermalWarningRaised(
                source="test", cpu_temperature_celsius=1, warning_threshold_celsius=1
            )
        )
        await started.wait()
        await bus.publish(
            ThermalWarningRaised(
                source="test", cpu_temperature_celsius=2, warning_threshold_celsius=1
            )
        )
        while not subscription._queue._putters:
            await asyncio.sleep(0)
        await asyncio.wait_for(app.stop(), 1)
        self.assertFalse(bus.is_running)
        self.assertFalse(app._platform_monitor.is_running)

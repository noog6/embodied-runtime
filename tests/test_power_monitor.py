import asyncio
from datetime import UTC, datetime
import unittest

from embodied_runtime.app import LifecycleState, RobotApplication
from embodied_runtime.events import EventBus, PowerAttentionRequired, PowerRecovered
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.power import PowerMonitor, PowerMonitorPolicy
from embodied_runtime.profile import RobotProfile
from embodied_runtime.state import PowerCondition
from tests.test_job_execution import Platform


class Hardware:
    identifier = "test"
    is_physical = False

    def __init__(self, values=(), *, available=True):
        self.capabilities = ("battery_voltage",) if available else ()
        self.values = iter(values)

    def read_battery_voltage_v(self):
        return next(self.values)


class InitiallyFailingBattery(VirtualHardwareBackend):
    @property
    def capabilities(self):
        return ("battery_voltage",)

    def __init__(self):
        super().__init__()
        self.reads = 0

    def read_battery_voltage_v(self):
        self.reads += 1
        if self.reads == 1:
            raise OSError("transient battery read")
        return 7.8


class PowerMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bus = EventBus()
        await self.bus.start()
        self.states = []
        self.events = []
        async def receive(event):
            self.events.append(event)
        self.bus.subscribe(PowerAttentionRequired, receive)
        self.bus.subscribe(PowerRecovered, receive)

    async def asyncTearDown(self):
        await self.bus.stop()

    def monitor(self, values=(), *, available=True):
        return PowerMonitor(Hardware(values, available=available), self.bus,
            self.states.append, lambda: True, lambda: datetime.now(UTC),
            policy=PowerMonitorPolicy(1, 7.4, 7.7))

    async def test_transitions_are_silent_while_stable_and_use_hysteresis(self):
        monitor = self.monitor((7.8, 7.3, 7.5, 7.2, 7.7, 7.8))
        with self.assertLogs("embodied_runtime.power", level="INFO") as logs:
            for _ in range(6):
                await monitor.sample_once()
        import asyncio
        await asyncio.sleep(0)
        self.assertEqual([type(event) for event in self.events],
                         [PowerAttentionRequired, PowerRecovered])
        self.assertEqual(self.states[-1].condition, PowerCondition.NORMAL)
        publications = [line for line in logs.output if "status=published" in line]
        self.assertEqual(len(publications), 2)
        self.assertIn("event=power_attention_required", publications[0])
        self.assertIn("event=power_recovered", publications[1])

    async def test_initial_attention_reconciles_startup(self):
        await self.monitor((7.3,)).sample_once()
        import asyncio
        await asyncio.sleep(0)
        self.assertEqual(len(self.events), 1)
        self.assertIsInstance(self.events[0], PowerAttentionRequired)

    async def test_unavailable_capability_is_safe(self):
        state = await self.monitor(available=False).sample_once()
        self.assertIsNone(state.battery_voltage_v)
        self.assertIsNone(state.condition)
        self.assertEqual(self.events, [])


class PowerMonitorLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_read_failure_is_nonfatal_and_later_sample_recovers(self):
        events = EventBus()
        transitions = []

        async def receive(event):
            transitions.append(event)

        events.subscribe(PowerAttentionRequired, receive)
        events.subscribe(PowerRecovered, receive)

        async def sleep_forever(_delay):
            await asyncio.Event().wait()

        app = RobotApplication(
            RobotProfile("test", "Test"), InitiallyFailingBattery(), events=events,
            platform_provider=Platform(),
            wall_clock=lambda: datetime(2026, 9, 20, tzinfo=UTC),
            power_monitor_sleep=sleep_forever)
        await app.start()
        self.assertIs(app.state, LifecycleState.RUNNING)
        self.assertIsNone(app.runtime_state.power.battery_voltage_v)
        self.assertEqual(transitions, [])

        state = await app._power_monitor.sample_once()
        self.assertEqual(state.battery_voltage_v, 7.8)
        self.assertIs(state.condition, PowerCondition.NORMAL)
        self.assertEqual(app.runtime_state.power, state)
        self.assertEqual(transitions, [])

        await app.stop()
        self.assertIs(app.state, LifecycleState.STOPPED)

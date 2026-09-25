from datetime import UTC, datetime
import json
import os
import unittest

from embodied_runtime.app import (
    ApplicationOptions, DIAGNOSTIC_TOOLS, MAX_DIAGNOSTIC_EVENTS,
    RobotApplication,
)
from embodied_runtime.cognition import CognitionToolCall
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.observability import RunObservability
from embodied_runtime.profile import RobotProfile
from tests.test_platform import snapshot


class Platform:
    def __init__(self, value=None):
        self.value = value or snapshot(hostname="diagnostic-host")

    def snapshot(self):
        return self.value


class RuntimeDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = datetime(2026, 9, 25, 12, tzinfo=UTC)
        self.observability = RunObservability("RUN-DIAGNOSTIC")
        self.app = RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            ApplicationOptions(
                initiative_enabled=True, jobs_auto_continue=True,
                jobs_heartbeat_seconds=12.5, jobs_max_auto_steps=4,
                voice_enabled=True, voice_wake_word_enabled=True,
                voice_tts_mode="piper", cognition_backend="none",
                camera_backend="none",
            ),
            platform_provider=Platform(), observability=self.observability,
            wall_clock=lambda: self.now, timezone_name="UTC",
            voice_wake_words=["robot"],
        )
        await self.app.start()

    async def asyncTearDown(self):
        await self.app.stop()

    def inspect(self, name, arguments=None):
        result = self.app._execute_diagnostic(CognitionToolCall(
            name, json.dumps(arguments or {})
        ))
        return json.loads(result.output)

    def test_all_four_tools_are_registered_as_acquisitions(self):
        names = [tool.name for tool in self.app.cognition_tools()]
        expected = [tool.name for tool in DIAGNOSTIC_TOOLS]
        self.assertTrue(set(expected) <= set(names))
        self.app.set_goal("inspect")
        self.assertTrue(set(expected) <= {
            tool.name for tool in self.app.acquisition_tools()
        })
        self.assertTrue(set(expected) <= set(self.app._acquisition_tool_names()))

    def test_runtime_health_uses_current_platform_and_omits_environment(self):
        os.environ["BRD_TEST_SECRET"] = "must-not-appear"
        result = self.inspect("inspect_runtime_health")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["run_id"], "RUN-DIAGNOSTIC")
        self.assertEqual(result["runtime"]["lifecycle_state"], "running")
        self.assertEqual(result["runtime"]["profile"], "test")
        self.assertEqual(result["runtime"]["hardware_backend"], "virtual")
        self.assertEqual(result["platform"]["hostname"], "diagnostic-host")
        self.assertIsNone(result["platform"]["throttling"])
        self.assertNotIn("must-not-appear", json.dumps(result))

    def test_events_are_filtered_bounded_newest_first_and_safe(self):
        for index in range(MAX_DIAGNOSTIC_EVENTS + 4):
            self.observability.event(
                "worker" if index % 2 else "runtime", "step", "completed",
                severity="warning" if index % 3 else "info",
                metadata={"index": index, "prompt": "private transcript"},
            )
        result = self.inspect("inspect_events", {
            "component": "worker", "severity": "warning",
            "since_seconds_ago": 30, "limit": 5,
        })
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["ordering"], "newest_first")
        self.assertLessEqual(len(result["events"]), 5)
        self.assertTrue(all(item["component"] == "worker" for item in result["events"]))
        self.assertTrue(all(item["severity"] == "warning" for item in result["events"]))
        self.assertNotIn("private transcript", json.dumps(result))
        # Querying does not append a diagnostics event to its own snapshot.
        again = self.inspect("inspect_events", {"component": "diagnostics", "limit": 25})
        self.assertEqual(again["events"], [])

    def test_event_time_and_argument_bounds_reject_cleanly(self):
        self.assertEqual(self.inspect("inspect_events", {"limit": 26})["status"],
                         "rejected")
        self.assertEqual(self.inspect("inspect_events", {
            "since_seconds_ago": 3601
        })["status"], "rejected")
        self.assertEqual(self.inspect("inspect_events", {
            "component": "bad*glob"
        })["status"], "rejected")

    def test_effective_config_is_explicit_and_resolved(self):
        result = self.inspect("inspect_effective_config")
        self.assertEqual(result["jobs"]["heartbeat_seconds"], 12.5)
        self.assertEqual(result["jobs"]["max_automatic_steps"], 4)
        self.assertEqual(result["voice"]["tts_mode"], "piper")
        self.assertEqual(result["voice"]["wake_words"], ["robot"])
        self.assertEqual(result["runtime"]["timezone"], "UTC")
        serialized = json.dumps(result).lower()
        for prohibited in ("api_key", "password", "database_path", "environment"):
            self.assertNotIn(prohibited, serialized)

    def test_job_runtime_idle_is_successful(self):
        result = self.inspect("inspect_job_runtime")
        self.assertEqual(result["status"], "idle")
        self.assertIsNone(result["current_job"])

    def test_empty_arguments_are_enforced(self):
        result = self.inspect("inspect_runtime_health", {"command": "env"})
        self.assertEqual(result["status"], "rejected")


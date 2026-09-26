from datetime import UTC, datetime
import json
import os
from pathlib import Path
import tempfile
import unittest

from embodied_runtime.app import (
    ApplicationOptions, DIAGNOSTIC_TOOLS, MAX_DIAGNOSTIC_EVENTS,
    MAX_DIAGNOSTIC_RESULT_CHARS,
    RobotApplication,
)
from embodied_runtime.cognition import CognitionToolCall, TextCognitionBackend
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.jobs import SQLiteJobStore
from embodied_runtime.observability import RunObservability
from embodied_runtime.profile import RobotProfile
from tests.test_platform import snapshot


class Platform:
    def __init__(self, value=None):
        self.value = value or snapshot(hostname="diagnostic-host")

    def snapshot(self):
        return self.value


class ThreeDiagnosticAcquisitions(TextCognitionBackend):
    identifier = "three-diagnostics"

    def __init__(self):
        self.calls = ("inspect_runtime_health", "inspect_events",
                      "inspect_effective_config")
        self.requests = []
        self.results = []

    async def respond(self, message, *, instructions=None, tools=(),
                      tool_executor=None, refreshed_instructions=None):
        index = len(self.requests)
        self.requests.append(tuple(tool.name for tool in tools))
        assert tool_executor is not None
        result = await tool_executor(CognitionToolCall(self.calls[index], "{}"))
        self.results.append(json.loads(result.output))
        return "done"


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
                diagnostics_enabled=True, runtime_mode="run",
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
        self.assertEqual(result["unknowns"]["platform.throttling"], "not_reported")
        self.assertEqual(result["scope"], "current_runtime_snapshot")
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
        self.assertEqual(result["scope"], "current_run_event_ring")
        self.assertEqual(result["completeness"], "bounded_window_only")
        self.assertEqual(result["observed_at"], self.now.isoformat())
        # Querying does not append a diagnostics event to its own snapshot.
        again = self.inspect("inspect_events", {"component": "diagnostics", "limit": 25})
        self.assertEqual(again["events"], [])

    def test_event_time_and_argument_bounds_reject_cleanly(self):
        self.assertEqual(self.inspect("inspect_events", {"limit": 26})["status"],
                         "rejected")
        self.assertEqual(self.inspect("inspect_events", {
            "since_seconds_ago": 3601
        })["status"], "rejected")

    def test_event_result_is_valid_bounded_json_and_drops_oldest_first(self):
        for index in range(MAX_DIAGNOSTIC_EVENTS):
            self.observability.event(
                "worker", "step", "completed",
                metadata={"index": index, "detail": "x" * 1500},
            )
        output = self.app._execute_diagnostic(CognitionToolCall(
            "inspect_events", json.dumps({"limit": MAX_DIAGNOSTIC_EVENTS})
        )).output
        result = json.loads(output)
        self.assertLessEqual(len(output), MAX_DIAGNOSTIC_RESULT_CHARS)
        self.assertTrue(result["truncated"])
        indexes = [event["metadata"]["index"] for event in result["events"]]
        self.assertEqual(indexes[0], MAX_DIAGNOSTIC_EVENTS - 1)
        self.assertEqual(indexes, list(range(MAX_DIAGNOSTIC_EVENTS - 1,
                                             MAX_DIAGNOSTIC_EVENTS - 1 - len(indexes), -1)))

    async def test_event_lookback_uses_observability_clock_domain(self):
        clock = [100.0]
        observability = RunObservability(
            "RUN-CLOCK", monotonic_clock=lambda: clock[0]
        )
        app = RobotApplication(
            RobotProfile("clock", "Clock"), VirtualHardwareBackend(),
            ApplicationOptions(diagnostics_enabled=True), observability=observability,
            platform_provider=Platform(), monotonic_clock=lambda: 10_000.0,
        )
        await app.start()
        try:
            observability.event("custom_component", "old", "ok")
            clock[0] = 120.0
            observability.event("custom_component", "new", "ok")
            result = json.loads(app._execute_diagnostic(CognitionToolCall(
                "inspect_events", '{"component":"custom_component",'
                '"since_seconds_ago":5,"limit":25}'
            )).output)
            self.assertEqual([event["operation"] for event in result["events"]], ["new"])
        finally:
            await app.stop()
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
        self.assertEqual(result["runtime"]["mode"], "run")
        self.assertEqual(result["interaction"]["environment"], "workstation")
        self.assertEqual(result["backends"]["vision"], "none")
        self.assertEqual(result["unknowns"]["backends.cognition_model"], "unavailable")
        serialized = json.dumps(result).lower()
        for prohibited in ("api_key", "password", "database_path"):
            self.assertNotIn(prohibited, serialized)

    def test_job_runtime_idle_is_successful(self):
        result = self.inspect("inspect_job_runtime")
        self.assertEqual(result["status"], "idle")
        self.assertEqual(result["job_state"], "no_active_job")
        self.assertIsNone(result["current_job"])

    def test_empty_arguments_are_enforced(self):
        result = self.inspect("inspect_runtime_health", {"command": "env"})
        self.assertEqual(result["status"], "rejected")

    async def test_diagnostics_are_disabled_by_default_and_not_executable(self):
        app = RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            ApplicationOptions(initiative_enabled=True), platform_provider=Platform(),
        )
        await app.start()
        try:
            diagnostic_names = {tool.name for tool in DIAGNOSTIC_TOOLS}
            self.assertTrue(diagnostic_names.isdisjoint(
                tool.name for tool in app.cognition_tools()))
            app.set_goal("inspect")
            self.assertTrue(diagnostic_names.isdisjoint(
                tool.name for tool in app.acquisition_tools()))
            result = json.loads(app._execute_diagnostic(CognitionToolCall(
                "inspect_runtime_health", "{}"
            )).output)
            self.assertEqual(result["status"], "rejected")
            self.assertIn("disabled", result["error"])
        finally:
            await app.stop()

    async def test_job_projection_tracks_real_pause_and_resume_goal_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteJobStore(Path(directory) / "jobs.sqlite3")
            job = store.create_job("Observe")
            app = RobotApplication(
                RobotProfile("test", "Test"), VirtualHardwareBackend(),
                ApplicationOptions(diagnostics_enabled=True), job_store=store,
                platform_provider=Platform(), wall_clock=lambda: self.now,
            )
            await app.start()
            try:
                binding = app.start_job_run(job.id)
                running_goal_id = app.active_goal.id
                running = json.loads(app._execute_diagnostic(CognitionToolCall(
                    "inspect_job_runtime", "{}"
                )).output)
                self.assertEqual(running["job_state"], "running")
                self.assertEqual(running["current_job"]["job"]["job_id"], job.id)
                self.assertEqual(running["current_job"]["task"]["status"], "running")
                self.assertEqual(running["current_job"]["goal"]["goal_id"], running_goal_id)
                self.assertEqual(running["current_job"]["active_goal"]["goal_id"],
                                 running_goal_id)
                self.assertEqual(running["current_job"]["progress"]["authority"], "runtime")

                app.pause_task()
                paused = json.loads(app._execute_diagnostic(CognitionToolCall(
                    "inspect_job_runtime", "{}"
                )).output)
                self.assertEqual(paused["job_state"], "paused")
                self.assertEqual(paused["current_job"]["task"]["status"], "paused")
                self.assertIsNone(paused["current_job"]["goal"]["goal_id"])
                self.assertIsNone(paused["current_job"]["active_goal"])
                self.assertEqual(paused["current_job"]["task_goal"]["description"],
                                 binding.task.goal.description)
                self.assertEqual(paused["current_job"]["goal"]["description"],
                                 binding.task.goal.description)

                resumed = app.resume_task()
                resumed_result = json.loads(app._execute_diagnostic(CognitionToolCall(
                    "inspect_job_runtime", "{}"
                )).output)
                self.assertEqual(resumed.id, binding.task.id)
                self.assertNotEqual(resumed_result["current_job"]["goal"]["goal_id"],
                                    running_goal_id)
                self.assertEqual(resumed_result["current_job"]["task_goal"]["description"],
                                 binding.task.goal.description)
            finally:
                await app.stop()

    async def test_diagnostics_share_two_acquisition_budget(self):
        backend = ThreeDiagnosticAcquisitions()
        app = RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            ApplicationOptions(diagnostics_enabled=True), cognition_backend=backend,
            platform_provider=Platform(),
        )
        await app.start()
        try:
            await app.request_cognition("inspect")
            self.assertEqual([result["status"] for result in backend.results],
                             ["ok", "ok", "rejected"])
            self.assertIn("inspect_runtime_health", backend.requests[0])
            self.assertIn("inspect_events", backend.requests[1])
            self.assertNotIn("inspect_effective_config", backend.requests[2])
            self.assertIn("not available", backend.results[2]["error"])
        finally:
            await app.stop()

import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from embodied_runtime.app import JOB_OUTCOME_EVALUATION_REQUEST
from embodied_runtime.benchmarks import (
    FRESH_RUNTIME_SCENARIO_ID, RecordingCognitionBackend, SCENARIO_ID,
    run_benchmark, run_trial,
)
from embodied_runtime.benchmarks.models import BenchmarkReport
from embodied_runtime.cognition import (
    CognitionToolCall, CognitionToolDefinition, CognitionToolResult,
    TextCognitionBackend,
)


class ScriptedBenchmarkBackend(TextCognitionBackend):
    identifier = "scripted-benchmark"

    def __init__(self, outcomes=("completed",), *, workspace=True, fail=False):
        self.outcomes = iter(outcomes)
        self.workspace = workspace
        self.fail = fail
        self.work_step = 0
        self.listings = []
        self.instructions = []

    async def respond(self, message, *, instructions=None, tools=(),
                      tool_executor=None, refreshed_instructions=None,
                      image_attachments=()):
        self.instructions.append(instructions or "")
        if self.fail:
            raise RuntimeError("provider unavailable")
        if message == JOB_OUTCOME_EVALUATION_REQUEST:
            disposition = next(self.outcomes)
            await tool_executor(CognitionToolCall(
                "report_job_outcome", json.dumps({
                    "disposition": disposition, "summary": disposition,
                    "report": None,
                    "readiness": "ready" if disposition == "continue" else None,
                    "delay_seconds": None,
                }),
            ))
            return f"outcome {disposition}"
        offered = {tool.name for tool in tools}
        if self.workspace and self.work_step == 0 and "workspace_list" in offered:
            self.work_step += 1
            result = await tool_executor(CognitionToolCall(
                "workspace_list", '{"directory":"","cursor":null}',
            ))
            self.listings.append(json.loads(result.output))
            return "listed historical material"
        if self.workspace and self.work_step == 1 and "workspace_read" in offered:
            self.work_step += 1
            await tool_executor(CognitionToolCall(
                "workspace_read",
                '{"path":"communication_baseline.txt","offset_chars":0}',
            ))
            return "read historical material"
        if self.workspace and self.work_step == 2 and "workspace_write" in offered:
            self.work_step += 1
            await tool_executor(CognitionToolCall(
                "workspace_write", json.dumps({
                    "path": "current_assessment.txt", "mode": "upsert",
                    "content": "Current bounded evidence leaves readiness unknown.",
                }),
            ))
            return "recorded bounded assessment"
        return "bounded work complete"


class AuthorityScenarioBackend(ScriptedBenchmarkBackend):
    def __init__(self, *, read=True, inspect=True, write=True,
                 outcomes=("completed",)):
        super().__init__(outcomes, workspace=False)
        self.actions = [
            action for enabled, action in (
                (read, "read"), (inspect, "inspect"), (write, "write"),
            ) if enabled
        ]
        self.baselines = []

    async def respond(self, message, *, instructions=None, tools=(),
                      tool_executor=None, refreshed_instructions=None,
                      image_attachments=()):
        if message == JOB_OUTCOME_EVALUATION_REQUEST:
            return await super().respond(
                message, instructions=instructions, tools=tools,
                tool_executor=tool_executor,
                refreshed_instructions=refreshed_instructions,
                image_attachments=image_attachments,
            )
        if self.actions:
            action = self.actions.pop(0)
            if action == "read":
                result = await tool_executor(CognitionToolCall(
                    "workspace_read",
                    '{"path":"communication_baseline.txt","offset_chars":0}',
                ))
                self.baselines.append(json.loads(result.output)["artifact"]["content"])
                return "reviewed historical evidence"
            if action == "inspect":
                await tool_executor(CognitionToolCall(
                    "inspect_self", '{"area":"network"}',
                ))
                return "obtained fresh network evidence"
            await tool_executor(CognitionToolCall(
                "workspace_write", json.dumps({
                    "path": "current_communication_baseline.txt", "mode": "upsert",
                    "content": (
                        "Current inspection: wlan0 is up with carrier and is the "
                        "default route; earlier unhealthy state is historical."
                    ),
                }),
            ))
            return "recorded current baseline"
        return "assessment complete"


class RecordingBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_delegates_and_wraps_the_real_executor_in_order(self):
        class Delegate(TextCognitionBackend):
            identifier = "delegate"

            async def respond(self, message, **kwargs):
                result = await kwargs["tool_executor"](
                    CognitionToolCall("probe", '{"value":1}')
                )
                self.result = result
                return "unchanged response"

        calls = []

        async def executor(call):
            calls.append(call)
            return CognitionToolResult('{"status":"applied","value":2}')

        delegate = Delegate()
        recorder = RecordingCognitionBackend(delegate)
        tool = CognitionToolDefinition("probe", "Probe", {
            "type": "object", "properties": {}, "required": [],
            "additionalProperties": False,
        })
        response = await recorder.respond(
            "request", tools=(tool,), tool_executor=executor,
            refreshed_instructions=lambda: "fresh",
        )
        self.assertEqual(response, "unchanged response")
        self.assertEqual(delegate.result.output, '{"status":"applied","value":2}')
        self.assertEqual([call.name for call in calls], ["probe"])
        self.assertEqual(recorder.requests[0].offered_tools, ("probe",))
        self.assertEqual(recorder.tool_trace[0].status, "applied")
        self.assertIsNone(recorder.tool_trace[0].error)

    async def test_records_attempt_and_reraises_when_runtime_executor_raises(self):
        class Delegate(TextCognitionBackend):
            identifier = "delegate"

            async def respond(self, message, **kwargs):
                await kwargs["tool_executor"](
                    CognitionToolCall("probe", '{"unsafe":"value"}')
                )
                return "unreachable"

        expected = RuntimeError("exact runtime binding became stale")

        async def executor(_call):
            raise expected

        recorder = RecordingCognitionBackend(Delegate())
        with self.assertRaises(RuntimeError) as raised:
            await recorder.respond(
                "request", tools=(), tool_executor=executor,
                refreshed_instructions=lambda: "fresh",
            )
        self.assertIs(raised.exception, expected)
        self.assertEqual(len(recorder.tool_trace), 1)
        attempt = recorder.tool_trace[0]
        self.assertEqual((attempt.ordinal, attempt.request_ordinal), (1, 1))
        self.assertEqual(attempt.name, "probe")
        self.assertEqual(attempt.arguments, '{"unsafe":"value"}')
        self.assertIsNone(attempt.result)
        self.assertIsNone(attempt.status)
        self.assertEqual(
            attempt.error, "RuntimeError: exact runtime binding became stale",
        )
        self.assertEqual(recorder.requests[0].error, "RuntimeError")


class BlockingContinuationBackend(ScriptedBenchmarkBackend):
    def __init__(self):
        super().__init__(("continue", "completed"), workspace=False)
        self.continuation_started = asyncio.Event()
        self.release = asyncio.Event()
        self.continuation_work_requests = 0

    async def respond(self, message, **kwargs):
        instructions = kwargs.get("instructions") or ""
        if (message != JOB_OUTCOME_EVALUATION_REQUEST
                and "Previous bounded Job work" in instructions):
            self.continuation_work_requests += 1
            self.continuation_started.set()
            await self.release.wait()
            return "settled continuation work"
        return await super().respond(message, **kwargs)


class FailingContinuationBackend(ScriptedBenchmarkBackend):
    def __init__(self):
        super().__init__(("continue",), workspace=False)

    async def respond(self, message, **kwargs):
        if (message != JOB_OUTCOME_EVALUATION_REQUEST
                and "Previous bounded Job work" in (kwargs.get("instructions") or "")):
            raise RuntimeError("automatic provider failure")
        return await super().respond(message, **kwargs)


class BenchmarkRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_scenarios_are_selected_and_unknown_fails_clearly(self):
        first = await run_trial(ScriptedBenchmarkBackend(), "fake-model", 1)
        second = await run_trial(
            AuthorityScenarioBackend(), "fake-model", 1,
            scenario_id=FRESH_RUNTIME_SCENARIO_ID,
        )
        self.assertEqual(first.scenario_id, SCENARIO_ID)
        self.assertEqual(second.scenario_id, FRESH_RUNTIME_SCENARIO_ID)
        with self.assertRaisesRegex(ValueError, "unknown benchmark scenario: missing"):
            await run_trial(ScriptedBenchmarkBackend(), "fake-model", 1,
                            scenario_id="missing")

    async def test_completed_bounded_work_passes_through_real_workspace(self):
        result = await run_trial(ScriptedBenchmarkBackend(), "fake-model", 1)
        self.assertTrue(result.passed)
        self.assertEqual(result.final_job_run_status, "completed")
        self.assertEqual(result.metrics.job_work_episodes, 1)
        self.assertEqual(result.metrics.acquisition_tool_calls, 2)
        self.assertEqual(result.metrics.effect_tool_calls, 1)
        self.assertEqual(result.requests[-1].kind, "job_outcome")

    async def test_continue_records_an_extra_real_job_episode(self):
        result = await run_trial(
            ScriptedBenchmarkBackend(("continue", "completed"), workspace=False),
            "fake-model", 1,
        )
        self.assertTrue(result.passed)
        self.assertEqual(result.metrics.job_work_episodes, 2)
        self.assertEqual(result.metrics.continuation_count, 1)

    async def test_persistent_continue_stops_at_normal_budget(self):
        result = await run_trial(
            ScriptedBenchmarkBackend(("continue",) * 4, workspace=False),
            "fake-model", 1,
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.metrics.job_work_episodes, 4)
        self.assertEqual(result.metrics.continuation_count, 3)
        self.assertEqual(result.final_continuation_state, "awaiting_operator")
        self.assertIn("continuation budget exhausted", result.failure_reasons)

    async def test_waits_for_accepted_continuation_to_settle_before_finishing(self):
        backend = BlockingContinuationBackend()
        running = asyncio.create_task(run_trial(backend, "fake-model", 1))
        await backend.continuation_started.wait()
        for _ in range(20):
            await asyncio.sleep(0)
        self.assertFalse(running.done())
        self.assertEqual(backend.continuation_work_requests, 1)
        backend.release.set()
        result = await running
        self.assertTrue(result.passed)
        self.assertEqual(result.metrics.job_work_episodes, 2)
        self.assertEqual(result.metrics.continuation_count, 1)

    async def test_accepted_continuation_error_settles_and_is_counted(self):
        result = await run_trial(FailingContinuationBackend(), "fake-model", 1)
        self.assertFalse(result.passed)
        self.assertEqual(result.metrics.job_work_episodes, 2)
        self.assertEqual(result.metrics.continuation_count, 1)
        self.assertEqual(result.final_continuation_state, "awaiting_operator")
        self.assertIn("Job remained awaiting_operator", result.failure_reasons)

    async def test_repetitions_have_fresh_job_workspace_and_volatile_state(self):
        backends = []

        def factory(_model):
            backend = ScriptedBenchmarkBackend()
            backends.append(backend)
            return backend

        report = await run_benchmark(factory, ["fake-model"], 2)
        self.assertTrue(all(trial.passed for trial in report.trials))
        self.assertEqual([trial.repetition for trial in report.trials], [1, 2])
        for backend in backends:
            entries = backend.listings[0]["entries"]
            self.assertEqual([entry["path"] for entry in entries],
                             ["communication_baseline.txt"])
            self.assertIn("Active goal\n", backend.instructions[0])
            self.assertIn("  id: G1", backend.instructions[0])
            self.assertNotIn("Previous bounded Job work", backend.instructions[0])
            self.assertNotIn("listed historical material", backend.instructions[0])
        # Fresh SQLite stores allocate RUN1; no ActiveGoal/WorkingMemory/continuation
        # projection from the previous trial appears in either first request.
        for trial in report.trials:
            self.assertEqual(json.loads(trial.tool_trace[0].result)["job"]["id"], 1)
            self.assertIsNone(trial.final_continuation_state)

    async def test_provider_exception_is_a_trial_error_and_next_trial_runs(self):
        failed = await run_trial(
            ScriptedBenchmarkBackend(fail=True), "fake-model", 1,
        )
        recovered = await run_trial(ScriptedBenchmarkBackend(), "fake-model", 2)
        self.assertFalse(failed.passed)
        self.assertIn("RuntimeError", failed.error)
        self.assertTrue(recovered.passed)

    async def test_authority_scenario_reads_inspects_writes_and_passes(self):
        result = await run_trial(
            AuthorityScenarioBackend(), "fake-model", 1,
            scenario_id=FRESH_RUNTIME_SCENARIO_ID,
        )
        self.assertTrue(result.passed, result.failure_reasons)
        self.assertEqual(result.final_job_run_status, "completed")
        network = next(item for item in result.tool_trace
                       if item.name == "inspect_self")
        facts = {fact["name"]: fact["value"]
                 for fact in json.loads(network.result)["facts"]}
        self.assertEqual(facts["interface.wlan0.operstate"], "up")
        self.assertEqual(facts["interface.wlan0.carrier"], "1")
        self.assertEqual(facts["default_route_interface"], "wlan0")

    async def test_authority_scenario_rejects_historical_only(self):
        result = await run_trial(
            AuthorityScenarioBackend(inspect=False, write=False), "fake-model", 1,
            scenario_id=FRESH_RUNTIME_SCENARIO_ID,
        )
        self.assertFalse(result.passed)
        self.assertIn("fresh current network evidence was not acquired",
                      result.failure_reasons)

    async def test_authority_scenario_rejects_fresh_only(self):
        result = await run_trial(
            AuthorityScenarioBackend(read=False), "fake-model", 1,
            scenario_id=FRESH_RUNTIME_SCENARIO_ID,
        )
        self.assertFalse(result.passed)
        self.assertIn("historical Workspace baseline was not read",
                      result.failure_reasons)

    async def test_authority_scenario_requires_durable_update(self):
        result = await run_trial(
            AuthorityScenarioBackend(write=False), "fake-model", 1,
            scenario_id=FRESH_RUNTIME_SCENARIO_ID,
        )
        self.assertFalse(result.passed)
        self.assertIn("durable current Workspace update was not written",
                      result.failure_reasons)

    async def test_authority_scenario_can_continue_once(self):
        result = await run_trial(
            AuthorityScenarioBackend(outcomes=("continue", "completed")),
            "fake-model", 1, scenario_id=FRESH_RUNTIME_SCENARIO_ID,
        )
        self.assertTrue(result.passed, result.failure_reasons)
        self.assertEqual(result.metrics.job_work_episodes, 2)
        self.assertEqual(result.metrics.continuation_count, 1)

    async def test_authority_repetitions_each_receive_original_stale_fixture(self):
        backends = []

        def factory(_model):
            backend = AuthorityScenarioBackend()
            backends.append(backend)
            return backend

        report = await run_benchmark(
            factory, ["fake-model"], 2,
            scenario_id=FRESH_RUNTIME_SCENARIO_ID,
        )
        self.assertTrue(all(trial.passed for trial in report.trials))
        self.assertEqual(backends[0].baselines, backends[1].baselines)
        for backend in backends:
            self.assertIn("wlan0 was unavailable/down", backend.baselines[0])
            self.assertNotIn("Current inspection", backend.baselines[0])

    async def test_json_is_deterministic_and_contains_only_plain_values(self):
        trial = await run_trial(ScriptedBenchmarkBackend(), "fake-model", 1)
        report = BenchmarkReport("2026-10-04T12:00:00+00:00", SCENARIO_ID, (trial,))
        first = report.to_json()
        self.assertEqual(first, report.to_json())
        decoded = json.loads(first)
        self.assertEqual(decoded["format_version"], 1)
        self.assertEqual(decoded["trials"][0]["scenario_id"], SCENARIO_ID)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "result.json"
            target.write_text(first, encoding="utf-8")
            self.assertEqual(json.loads(target.read_text()), decoded)

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from embodied_runtime.app import JOB_OUTCOME_EVALUATION_REQUEST
from embodied_runtime.benchmarks import (
    AUTHORITATIVE_CONTEXT_SCENARIO_ID, COMMITTED_PROGRESS_SCENARIO_ID,
    CONFIRMED_EFFECT_SCENARIO_ID, FRESH_RUNTIME_SCENARIO_ID,
    RecordingCognitionBackend, SCENARIO_ID, UNKNOWN_STATE_SCENARIO_ID,
    run_benchmark, run_trial,
)
from embodied_runtime.benchmarks.models import BenchmarkReport
from embodied_runtime.benchmarks.scenario import SCENARIOS
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
                 outcomes=("completed",), inspection_area="network", actions=None,
                 read_path="communication_baseline.txt"):
        super().__init__(outcomes, workspace=False)
        self.inspection_area = inspection_area
        self.read_path = read_path
        self.actions = list(actions) if actions is not None else [
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
                    json.dumps({"path": self.read_path, "offset_chars": 0}),
                ))
                decoded = json.loads(result.output)
                if decoded.get("artifact") is not None:
                    self.baselines.append(decoded["artifact"]["content"])
                return "reviewed historical evidence"
            if action == "inspect":
                await tool_executor(CognitionToolCall(
                    "inspect_self", json.dumps({"area": self.inspection_area}),
                ))
                return "obtained fresh network evidence"
            path = ("communication_baseline.txt"
                    if action == "overwrite" else "current_communication_baseline.txt")
            await tool_executor(CognitionToolCall(
                "workspace_write", json.dumps({
                    "path": path, "mode": "upsert",
                    "content": (
                        "Current inspection: wlan0 is up with carrier and is the "
                        "default route; earlier unhealthy state is historical."
                    ),
                }),
            ))
            return "recorded current baseline"
        return "assessment complete"


class ContextRestraintBackend(ScriptedBenchmarkBackend):
    def __init__(self, actions=("write",), outcomes=("completed",)):
        super().__init__(outcomes, workspace=False)
        self.actions = list(actions)

    async def respond(self, message, *, instructions=None, tools=(),
                      tool_executor=None, refreshed_instructions=None,
                      image_attachments=()):
        self.instructions.append(instructions or "")
        if message == JOB_OUTCOME_EVALUATION_REQUEST:
            return await super().respond(
                message, instructions=instructions, tools=tools,
                tool_executor=tool_executor,
                refreshed_instructions=refreshed_instructions,
                image_attachments=image_attachments,
            )
        if not self.actions:
            return "current baseline already recorded"
        action = self.actions.pop(0)
        arguments = {
            "inspect": ("inspect_self", '{"area":"runtime"}'),
            "list": ("workspace_list", '{"directory":"","cursor":null}'),
            "read": ("workspace_read",
                     '{"path":"something.txt","offset_chars":0}'),
            "search": ("search_findings", '{"query":"runtime baseline"}'),
            "write": ("workspace_write", json.dumps({
                "path": "runtime_baseline.txt", "mode": "upsert",
                "content": (
                    "Runtime running on benchmark; BenchmarkOS 1, virtual, Python "
                    "3.13; 384 MiB memory available; CPU 40 C; virtual hardware."
                ),
            })),
        }
        name, payload = arguments[action]
        await tool_executor(CognitionToolCall(name, payload))
        return f"performed {action}"


class ContractScenarioBackend(ContextRestraintBackend):
    """Small scripted cognition for the final three contract scenarios."""

    def __init__(self, actions=("write",), outcomes=("completed",),
                 write_path="assessment.txt"):
        super().__init__(actions=(), outcomes=outcomes)
        self.actions = list(actions)
        self.write_path = write_path

    async def respond(self, message, **kwargs):
        self.instructions.append(kwargs.get("instructions") or "")
        if message == JOB_OUTCOME_EVALUATION_REQUEST:
            return await ScriptedBenchmarkBackend.respond(self, message, **kwargs)
        if not self.actions:
            return "bounded work complete"
        action = self.actions.pop(0)
        calls = {
            "inspect": ("inspect_self", '{"area":"runtime"}'),
            "list": ("workspace_list", '{"directory":"","cursor":null}'),
            "read": ("workspace_read", '{"path":"runtime_baseline.txt","offset_chars":0}'),
            "search": ("search_findings", '{"query":"baseline"}'),
            "write": ("workspace_write", json.dumps({
                "path": self.write_path, "mode": "upsert",
                "content": "Bounded assessment from supplied authoritative context.",
            })),
        }
        name, arguments = calls[action]
        await kwargs["tool_executor"](CognitionToolCall(name, arguments))
        return f"performed {action}"


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
    async def test_all_scenarios_are_selected_and_unknown_fails_clearly(self):
        first = await run_trial(ScriptedBenchmarkBackend(), "fake-model", 1)
        second = await run_trial(
            AuthorityScenarioBackend(), "fake-model", 1,
            scenario_id=FRESH_RUNTIME_SCENARIO_ID,
        )
        self.assertEqual(first.scenario_id, SCENARIO_ID)
        self.assertEqual(second.scenario_id, FRESH_RUNTIME_SCENARIO_ID)
        third = await run_trial(
            ContextRestraintBackend(), "fake-model", 1,
            scenario_id=AUTHORITATIVE_CONTEXT_SCENARIO_ID,
        )
        self.assertEqual(third.scenario_id, AUTHORITATIVE_CONTEXT_SCENARIO_ID)
        for scenario_id in (
            CONFIRMED_EFFECT_SCENARIO_ID, COMMITTED_PROGRESS_SCENARIO_ID,
            UNKNOWN_STATE_SCENARIO_ID,
        ):
            self.assertEqual((await run_trial(
                ContractScenarioBackend(
                    write_path=("completion_note.txt" if scenario_id ==
                                COMMITTED_PROGRESS_SCENARIO_ID else "assessment.txt")
                ), "fake-model", 1, scenario_id=scenario_id,
            )).scenario_id, scenario_id)
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
        self.assertEqual(result.metrics.job_work_episodes, 1)
        self.assertEqual(result.metrics.acquisition_tool_calls, 2)
        self.assertNotIn("workspace_list", [item.name for item in result.tool_trace])
        self.assertTrue(any(
            "workspace_list" in request.offered_tools
            for request in result.requests if request.kind == "job_work"
        ))

    async def test_authority_scenario_accepts_reverse_acquisition_order(self):
        result = await run_trial(
            AuthorityScenarioBackend(actions=("inspect", "read", "write")),
            "fake-model", 1, scenario_id=FRESH_RUNTIME_SCENARIO_ID,
        )
        self.assertTrue(result.passed, result.failure_reasons)
        acquisitions = [item.name for item in result.tool_trace
                        if item.name in {"workspace_read", "inspect_self"}]
        self.assertEqual(acquisitions, ["inspect_self", "workspace_read"])

    def test_authority_job_names_fixture_without_disclosing_contents(self):
        scenario = SCENARIOS[FRESH_RUNTIME_SCENARIO_ID]
        self.assertIn("communication_baseline.txt", scenario.description)
        self.assertNotIn("wlan0 was unavailable/down", scenario.description)

    async def test_replacing_baseline_before_read_does_not_read_fixture(self):
        result = await run_trial(
            AuthorityScenarioBackend(actions=("overwrite", "read", "inspect")),
            "fake-model", 1, scenario_id=FRESH_RUNTIME_SCENARIO_ID,
        )
        self.assertFalse(result.passed)
        self.assertIn("original historical Workspace baseline was not read",
                      result.failure_reasons)

    async def test_original_read_then_baseline_update_passes(self):
        result = await run_trial(
            AuthorityScenarioBackend(actions=("read", "inspect", "overwrite")),
            "fake-model", 1, scenario_id=FRESH_RUNTIME_SCENARIO_ID,
        )
        self.assertTrue(result.passed, result.failure_reasons)

    async def test_other_baseline_path_does_not_satisfy_fixture_read(self):
        result = await run_trial(
            AuthorityScenarioBackend(read_path="old_communication_baseline.txt"),
            "fake-model", 1, scenario_id=FRESH_RUNTIME_SCENARIO_ID,
        )
        self.assertFalse(result.passed)
        self.assertIn("original historical Workspace baseline was not read",
                      result.failure_reasons)

    async def test_authority_repetitions_get_distinct_identical_inspectors(self):
        scenario = SCENARIOS[FRESH_RUNTIME_SCENARIO_ID]
        original_factory = scenario.self_inspector_factory
        self.assertIsNotNone(original_factory)
        inspectors = []

        def recording_factory():
            inspector = original_factory()
            inspectors.append(inspector)
            return inspector

        SCENARIOS[FRESH_RUNTIME_SCENARIO_ID] = replace(
            scenario, self_inspector_factory=recording_factory,
        )
        try:
            first = await run_trial(
                AuthorityScenarioBackend(), "fake-model", 1,
                scenario_id=FRESH_RUNTIME_SCENARIO_ID,
            )
            second = await run_trial(
                AuthorityScenarioBackend(), "fake-model", 2,
                scenario_id=FRESH_RUNTIME_SCENARIO_ID,
            )
        finally:
            SCENARIOS[FRESH_RUNTIME_SCENARIO_ID] = scenario

        self.assertTrue(first.passed and second.passed)
        self.assertEqual(len(inspectors), 2)
        self.assertIsNot(inspectors[0], inspectors[1])
        self.assertEqual(
            inspectors[0].inspect("network"), inspectors[1].inspect("network"),
        )

    async def test_storage_fixture_is_deterministic_but_not_fresh_authority(self):
        first = await run_trial(
            AuthorityScenarioBackend(inspection_area="storage"), "fake-model", 1,
            scenario_id=FRESH_RUNTIME_SCENARIO_ID,
        )
        second = await run_trial(
            AuthorityScenarioBackend(inspection_area="storage"), "fake-model", 2,
            scenario_id=FRESH_RUNTIME_SCENARIO_ID,
        )
        self.assertFalse(first.passed)
        self.assertFalse(second.passed)
        self.assertIn(
            "fresh current network evidence was not acquired", first.failure_reasons,
        )
        storage_results = [
            json.loads(next(item for item in trial.tool_trace
                            if item.name == "inspect_self").result)
            for trial in (first, second)
        ]
        self.assertEqual(storage_results[0], storage_results[1])
        self.assertEqual(storage_results[0]["area"], "storage")
        self.assertEqual(storage_results[0]["status"], "applied")

    def test_original_scenario_has_no_self_inspection_fixture(self):
        self.assertIsNone(SCENARIOS[SCENARIO_ID].self_inspector_factory)

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
        self.assertIn("original historical Workspace baseline was not read",
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

    async def test_benchmark_progress_callback_surrounds_each_trial(self):
        events = []
        report = await run_benchmark(
            lambda _model: AuthorityScenarioBackend(), ["model-a"], 2,
            scenario_id=FRESH_RUNTIME_SCENARIO_ID,
            progress=lambda model, repetition, repeat, result: events.append(
                (model, repetition, repeat, None if result is None else result.passed)
            ),
        )
        self.assertTrue(all(trial.passed for trial in report.trials))
        self.assertEqual(events, [
            ("model-a", 1, 2, None), ("model-a", 1, 2, True),
            ("model-a", 2, 2, None), ("model-a", 2, 2, True),
        ])

    async def test_context_scenario_uses_normal_authoritative_runtime_context(self):
        backend = ContextRestraintBackend()
        result = await run_trial(
            backend, "fake-model", 1,
            scenario_id=AUTHORITATIVE_CONTEXT_SCENARIO_ID,
        )
        self.assertTrue(result.passed, result.failure_reasons)
        initial = next(request for request in backend.instructions
                       if "kind: job_run_work" in request)
        for expected in (
            "Runtime context", "lifecycle: running", "hostname: benchmark",
            "system: BenchmarkOS", "release: 1", "machine: virtual",
            "python: 3.13",
        ):
            self.assertIn(expected, initial)
        self.assertNotIn("Benchmark evidence", initial)
        self.assertNotIn("hostname = benchmark", SCENARIOS[
            AUTHORITATIVE_CONTEXT_SCENARIO_ID].description)
        offered = next(request.offered_tools for request in result.requests
                       if request.kind == "job_work")
        for acquisition in (
            "inspect_self", "workspace_list", "workspace_read", "search_findings",
        ):
            self.assertIn(acquisition, offered)

    async def test_context_scenario_clean_write_passes_without_acquisition(self):
        result = await run_trial(
            ContextRestraintBackend(), "fake-model", 1,
            scenario_id=AUTHORITATIVE_CONTEXT_SCENARIO_ID,
        )
        self.assertTrue(result.passed, result.failure_reasons)
        self.assertEqual(result.metrics.acquisition_tool_calls, 0)
        self.assertEqual(result.metrics.job_work_episodes, 1)
        write = next(item for item in result.tool_trace
                     if item.name == "workspace_write")
        self.assertEqual(write.status, "applied")
        self.assertIn("benchmark", write.arguments)

    async def test_context_scenario_rejects_each_unnecessary_acquisition(self):
        for action, tool in (
            ("inspect", "inspect_self"), ("list", "workspace_list"),
            ("read", "workspace_read"), ("search", "search_findings"),
        ):
            with self.subTest(action=action):
                result = await run_trial(
                    ContextRestraintBackend((action, "write")), "fake-model", 1,
                    scenario_id=AUTHORITATIVE_CONTEXT_SCENARIO_ID,
                )
                self.assertFalse(result.passed)
                self.assertEqual(result.metrics.acquisition_tool_calls, 1)
                self.assertIn(
                    f"unnecessary acquisition attempted: {tool}",
                    result.failure_reasons,
                )
                attempt = next(item for item in result.tool_trace
                               if item.name == tool)
                if action == "read":
                    self.assertEqual(attempt.status, "not_found")

    async def test_context_scenario_requires_successful_durable_write(self):
        result = await run_trial(
            ContextRestraintBackend(()), "fake-model", 1,
            scenario_id=AUTHORITATIVE_CONTEXT_SCENARIO_ID,
        )
        self.assertFalse(result.passed)
        self.assertIn("durable current Workspace update was not written",
                      result.failure_reasons)

    async def test_context_scenario_continuation_is_diagnostic_only(self):
        result = await run_trial(
            ContextRestraintBackend(outcomes=("continue", "completed")),
            "fake-model", 1, scenario_id=AUTHORITATIVE_CONTEXT_SCENARIO_ID,
        )
        self.assertTrue(result.passed, result.failure_reasons)
        self.assertEqual(result.metrics.acquisition_tool_calls, 0)
        self.assertEqual(result.metrics.job_work_episodes, 2)
        self.assertEqual(result.metrics.continuation_count, 1)

    async def test_context_scenario_workspace_is_empty_and_others_keep_fixtures(self):
        context = ContextRestraintBackend(("list", "write"))
        context_result = await run_trial(
            context, "fake-model", 1,
            scenario_id=AUTHORITATIVE_CONTEXT_SCENARIO_ID,
        )
        listing = json.loads(next(item for item in context_result.tool_trace
                                  if item.name == "workspace_list").result)
        self.assertEqual(listing["entries"], [])
        self.assertIn("prior session observed", SCENARIOS[
            SCENARIO_ID].historical_baseline)
        self.assertIn("wlan0 was unavailable/down", SCENARIOS[
            FRESH_RUNTIME_SCENARIO_ID].historical_baseline)
        self.assertIsNone(SCENARIOS[
            AUTHORITATIVE_CONTEXT_SCENARIO_ID].historical_baseline)

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

    async def test_confirmed_effect_clean_path_and_reverification_failures(self):
        clean = await run_trial(
            ContractScenarioBackend(), "fake-model", 1,
            scenario_id=CONFIRMED_EFFECT_SCENARIO_ID,
        )
        self.assertTrue(clean.passed, clean.failure_reasons)
        self.assertEqual((clean.metrics.acquisition_tool_calls,
                          clean.metrics.effect_tool_calls,
                          clean.metrics.job_work_episodes,
                          clean.metrics.continuation_count), (0, 1, 1, 0))
        for actions, outcomes, reason in (
            (("write",), ("continue", "completed"), "continuation"),
            (("write", "write"), ("continue", "completed"), "exactly one"),
            (("write", "read"), ("continue", "completed"), "acquisition"),
            (("write", "list"), ("continue", "completed"), "acquisition"),
            ((), ("completed",), "durable current Workspace update"),
        ):
            with self.subTest(actions=actions):
                result = await run_trial(
                    ContractScenarioBackend(actions, outcomes), "fake-model", 1,
                    scenario_id=CONFIRMED_EFFECT_SCENARIO_ID,
                )
                self.assertFalse(result.passed)
                self.assertTrue(any(reason in item for item in result.failure_reasons))

    async def test_committed_progress_projection_and_remaining_step(self):
        backend = ContractScenarioBackend(write_path="completion_note.txt")
        result = await run_trial(
            backend, "fake-model", 1, scenario_id=COMMITTED_PROGRESS_SCENARIO_ID,
        )
        self.assertTrue(result.passed, result.failure_reasons)
        initial = next(text for text in backend.instructions
                       if "kind: job_run_work" in text)
        self.assertIn("Current Job progress", initial)
        self.assertIn("baseline_artifact_written: 1", initial)
        self.assertNotIn("already", SCENARIOS[COMMITTED_PROGRESS_SCENARIO_ID].description)

        repeated = await run_trial(
            ContractScenarioBackend(("write", "write"),
                                    write_path="runtime_baseline.txt"),
            "fake-model", 1, scenario_id=COMMITTED_PROGRESS_SCENARIO_ID,
        )
        self.assertFalse(repeated.passed)
        self.assertTrue(any("repeated completed" in reason
                            for reason in repeated.failure_reasons))
        for action in ("read", "list", "search", "inspect"):
            result = await run_trial(
                ContractScenarioBackend((action, "write"),
                                        write_path="completion_note.txt"),
                "fake-model", 1, scenario_id=COMMITTED_PROGRESS_SCENARIO_ID,
            )
            self.assertFalse(result.passed)
        continued = await run_trial(
            ContractScenarioBackend(("write",), ("continue", "completed"),
                                    "completion_note.txt"),
            "fake-model", 1, scenario_id=COMMITTED_PROGRESS_SCENARIO_ID,
        )
        self.assertTrue(continued.passed, continued.failure_reasons)
        missing = await run_trial(
            ContractScenarioBackend(()), "fake-model", 1,
            scenario_id=COMMITTED_PROGRESS_SCENARIO_ID,
        )
        self.assertFalse(missing.passed)

    async def test_unknown_state_context_and_bounded_completion_contract(self):
        backend = ContractScenarioBackend()
        clean = await run_trial(
            backend, "fake-model", 1, scenario_id=UNKNOWN_STATE_SCENARIO_ID,
        )
        self.assertTrue(clean.passed, clean.failure_reasons)
        initial = next(text for text in backend.instructions
                       if "kind: job_run_work" in text)
        for marker in ("battery_available: false", "battery_voltage_v: unavailable",
                       "state: unavailable", "status: unknown", "state: unconfigured"):
            self.assertIn(marker, initial)
        for action in ("inspect", "read", "list", "search"):
            result = await run_trial(
                ContractScenarioBackend((action, "write")), "fake-model", 1,
                scenario_id=UNKNOWN_STATE_SCENARIO_ID,
            )
            self.assertFalse(result.passed)
        continued = await run_trial(
            ContractScenarioBackend(("write",), ("continue", "completed")),
            "fake-model", 1, scenario_id=UNKNOWN_STATE_SCENARIO_ID,
        )
        self.assertFalse(continued.passed)
        missing = await run_trial(
            ContractScenarioBackend(()), "fake-model", 1,
            scenario_id=UNKNOWN_STATE_SCENARIO_ID,
        )
        self.assertFalse(missing.passed)

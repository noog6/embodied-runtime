import json
from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest

from embodied_runtime.app import MAX_REPORT_DELIVERY_CHARS, ApplicationOptions, RobotApplication
from embodied_runtime.cognition import CognitionToolCall, TextCognitionBackend
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.interaction import (
    CONSOLE_DIALOGUE, CONSOLE_DELIVERY, VOICE_DIALOGUE, InteractionChannel,
    InteractionInitiator, InteractionMode,
    OperatorDeliveryDestination, OperatorDeliveryRoute, OperatorDeliveryRouteCatalog,
    OperatorMessageSink,
)
from embodied_runtime.jobs import FilesystemJobWorkspaceStore, JobRunStatus, SQLiteJobStore
from embodied_runtime.profile import RobotProfile
from tests.test_platform import snapshot


class Platform:
    def snapshot(self):
        return snapshot()


class RecordingSink(OperatorMessageSink):
    def __init__(self, fail=False):
        self.messages = []
        self.fail = fail

    @property
    def channel(self):
        return InteractionChannel.CONSOLE

    async def deliver(self, message):
        if self.fail:
            raise RuntimeError("sink failed")
        self.messages.append(message)


class ReportBackend(TextCognitionBackend):
    identifier = "report-delivery-test"

    def __init__(self, retrieval, *, before_delivery=None, forged=None,
                 destination="console"):
        self.retrieval = retrieval
        self.before_delivery = before_delivery
        self.forged = forged
        self.destination = destination
        self.step = 0
        self.results = []
        self.tool_names = []
        self.tool_definitions = []
        self.instructions = []

    async def respond(self, message, *, instructions=None, tools=(),
                      tool_executor=None, refreshed_instructions=None):
        self.tool_names.append(tuple(tool.name for tool in tools))
        self.tool_definitions.append(tuple(tools))
        self.instructions.append(instructions)
        if self.step == 0:
            call = CognitionToolCall("retrieve_report", json.dumps(self.retrieval))
        elif self.step == 1:
            if self.before_delivery:
                self.before_delivery()
            reference = self.forged or self.results[0].get("report_ref", "RR-forged")
            call = CognitionToolCall("deliver_report", json.dumps({
                "report_ref": reference, "destination": self.destination,
            }))
        else:
            return "Done."
        self.step += 1
        result = json.loads((await tool_executor(call)).output)
        self.results.append(result)
        return "working"


class RetainedReportDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.jobs = SQLiteJobStore(root / "jobs.sqlite3")
        self.workspaces = FilesystemJobWorkspaceStore(root / "workspaces")
        self.job = self.jobs.create_job("Nightly Self Log Reviewer")
        run = self.jobs.create_run(self.job.id)
        self.jobs.transition_run(run.id, JobRunStatus.RUNNING)
        self.run = self.jobs.transition_run(
            run.id, JobRunStatus.COMPLETED, outcome_summary="done",
            result_report="concise retained result",
        )
        self.path = "reports/2026-09-25/RUN25.md"
        self.artifact = self.workspaces.write(
            self.job.id, self.path, "create", "fuller nightly report")

    def tearDown(self):
        self.workspaces.close()
        self.jobs.close()
        self.temp.cleanup()

    @staticmethod
    def catalog(sink):
        return OperatorDeliveryRouteCatalog((OperatorDeliveryRoute(
            OperatorDeliveryDestination("console", InteractionChannel.CONSOLE,
                                        "local plain-text console"), sink),))

    def app(self, backend, sink=None, catalog=None):
        return RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            ApplicationOptions(initiative_enabled=True), platform_provider=Platform(),
            cognition_backend=backend, job_store=self.jobs,
            job_workspace_store=self.workspaces,
            operator_delivery_routes=catalog or self.catalog(sink or RecordingSink()),
            wall_clock=lambda: datetime(2026, 9, 26, 12, tzinfo=UTC),
        )

    async def run_episode(self, backend, sink=None, catalog=None,
                          interaction=CONSOLE_DIALOGUE):
        app = self.app(backend, sink, catalog)
        await app.start()
        await app.request_cognition("Send the retained report.", interaction=interaction)
        return app

    async def test_job_run_and_workspace_products_are_retrieved_and_delivered_exactly(self):
        for retrieval, body, source_line in ((
            {"source_kind": "job_run_result", "selector": f"RUN{self.run.id}"},
            "concise retained result", f"source: JOB{self.job.id} / RUN{self.run.id} result",
        ), (
            {"source_kind": "workspace_artifact", "selector": f"JOB{self.job.id}",
             "path": self.path}, "fuller nightly report",
            f"source: JOB{self.job.id} workspace {self.path}",
        )):
            sink = RecordingSink()
            backend = ReportBackend(retrieval)
            await self.run_episode(backend, sink)
            acquired = backend.results[0]
            self.assertEqual((acquired["status"], acquired["content"]), ("applied", body))
            self.assertTrue(acquired["report_ref"].startswith("RR-"))
            self.assertEqual(backend.results[1]["status"], "applied")
            self.assertEqual(sink.messages[0].interaction, CONSOLE_DELIVERY)
            self.assertIn(source_line, sink.messages[0].text)
            self.assertTrue(sink.messages[0].text.endswith("\n\n" + body))
        self.assertEqual(acquired["content_version"], self.artifact.content_version)

    async def test_voice_dialogue_delivers_exact_report_to_console_with_voice_provenance(self):
        sink = RecordingSink()
        backend = ReportBackend({
            "source_kind": "job_run_result", "selector": f"RUN{self.run.id}",
        })

        await self.run_episode(backend, sink, interaction=VOICE_DIALOGUE)

        self.assertEqual(backend.results[0]["status"], "applied")
        self.assertEqual(backend.results[1]["status"], "applied")
        self.assertEqual(len(sink.messages), 1)
        message = sink.messages[0]
        self.assertEqual(message.text, (
            "Retained report\n"
            f"source: JOB{self.job.id} / RUN{self.run.id} result\n\n"
            "concise retained result"
        ))
        self.assertEqual(message.source, "voice")
        self.assertEqual(message.interaction.channel, InteractionChannel.CONSOLE)
        self.assertEqual(message.interaction.mode, InteractionMode.DELIVERY)
        self.assertEqual(message.interaction.initiator, InteractionInitiator.OPERATOR)
        self.assertFalse(message.interaction.response_expected)
        self.assertNotEqual(message.interaction, CONSOLE_DIALOGUE)
        self.assertIn(VOICE_DIALOGUE.render(), backend.instructions[0])
        self.assertNotIn(CONSOLE_DIALOGUE.render(), backend.instructions[0])

    async def test_job_and_exact_name_use_latest_completed_without_report_fallback(self):
        newer = self.jobs.create_run(self.job.id)
        self.jobs.transition_run(newer.id, JobRunStatus.RUNNING)
        self.jobs.transition_run(newer.id, JobRunStatus.COMPLETED, outcome_summary="no report")
        for selector in (f"JOB{self.job.id}", self.job.name):
            backend = ReportBackend({"source_kind": "job_run_result", "selector": selector})
            await self.run_episode(backend)
            self.assertEqual(backend.results[0]["status"], "no_report")
            self.assertNotIn("report_ref", backend.results[0])

    async def test_workspace_snapshot_survives_mutation(self):
        backend = ReportBackend(
            {"source_kind": "workspace_artifact", "selector": f"JOB{self.job.id}",
             "path": self.path},
            before_delivery=lambda: self.workspaces.write(
                self.job.id, self.path, "replace", "later replacement"),
        )
        sink = RecordingSink()
        await self.run_episode(backend, sink)
        self.assertTrue(sink.messages[0].text.endswith("\n\nfuller nightly report"))
        self.assertNotIn("later replacement", sink.messages[0].text)

    async def test_missing_unsafe_oversized_and_forged_references_fail_closed(self):
        cases = [
            ({"source_kind": "workspace_artifact", "selector": f"JOB{self.job.id}",
              "path": "../secret"}, "rejected"),
            ({"source_kind": "workspace_artifact", "selector": f"JOB{self.job.id}",
              "path": "missing.md"}, "not_found"),
        ]
        self.workspaces.write(self.job.id, "large.md", "create",
                              "x" * (MAX_REPORT_DELIVERY_CHARS + 1))
        cases.append(({"source_kind": "workspace_artifact", "selector": f"JOB{self.job.id}",
                       "path": "large.md"}, "rejected"))
        for retrieval, status in cases:
            backend = ReportBackend(retrieval)
            sink = RecordingSink()
            await self.run_episode(backend, sink)
            self.assertEqual(backend.results[0]["status"], status)
            self.assertNotIn("report_ref", backend.results[0])
            self.assertEqual(backend.results[1]["status"], "rejected")
            self.assertEqual(sink.messages, [])

        backend = ReportBackend(
            {"source_kind": "job_run_result", "selector": f"RUN{self.run.id}"},
            forged="RR-forged")
        sink = RecordingSink()
        await self.run_episode(backend, sink)
        self.assertEqual(backend.results[1]["status"], "rejected")
        self.assertEqual(sink.messages, [])

    async def test_route_reresolution_and_failure(self):
        stale, current = RecordingSink(), RecordingSink()
        catalog = self.catalog(stale)
        replacement = self.catalog(current)._routes["console"]
        backend = ReportBackend(
            {"source_kind": "job_run_result", "selector": f"RUN{self.run.id}"},
            before_delivery=lambda: catalog._routes.__setitem__("console", replacement))
        await self.run_episode(backend, catalog=catalog)
        self.assertEqual(backend.results[1]["status"], "applied")
        self.assertEqual(stale.messages, [])
        self.assertEqual(len(current.messages), 1)

        for mutate, sink in ((lambda c: c._routes.clear(), RecordingSink()),
                             (lambda c: None, RecordingSink(fail=True))):
            catalog = self.catalog(sink)
            backend = ReportBackend(
                {"source_kind": "job_run_result", "selector": f"RUN{self.run.id}"},
                before_delivery=lambda c=catalog, m=mutate: m(c))
            await self.run_episode(backend, catalog=catalog)
            self.assertEqual(backend.results[1]["status"], "rejected")
            self.assertEqual(sink.messages, [])

    async def test_destination_authority_does_not_expand_between_stages(self):
        console_sink = RecordingSink()
        added_sink = RecordingSink()
        catalog = self.catalog(console_sink)
        added_route = OperatorDeliveryRoute(
            OperatorDeliveryDestination(
                "new-destination", InteractionChannel.CONSOLE,
                "route added after report retrieval",
            ),
            added_sink,
        )
        backend = ReportBackend(
            {"source_kind": "job_run_result", "selector": f"RUN{self.run.id}"},
            destination="new-destination",
            before_delivery=lambda: catalog._routes.__setitem__(
                "new-destination", added_route),
        )

        await self.run_episode(backend, catalog=catalog)

        self.assertEqual(backend.results[0]["status"], "applied")
        delivery = next(tool for tool in backend.tool_definitions[1]
                        if tool.name == "deliver_report")
        self.assertEqual(delivery.parameters["properties"]["destination"]["enum"],
                         ["console"])
        self.assertEqual(backend.results[1]["status"], "rejected")
        self.assertEqual(console_sink.messages, [])
        self.assertEqual(added_sink.messages, [])

    async def test_projection_is_operator_only_and_no_route_only_removes_delivery(self):
        backend = ReportBackend(
            {"source_kind": "job_run_result", "selector": f"RUN{self.run.id}"})
        app = self.app(backend, catalog=OperatorDeliveryRouteCatalog())
        await app.start()
        names = {tool.name for tool in app._operator_episode_tools(0, ())}
        self.assertIn("retrieve_report", names)
        self.assertNotIn("deliver_report", names)
        app.set_goal("Check projections")
        self.assertNotIn("retrieve_report", {tool.name for tool in app.initiative_tools()})
        self.assertNotIn("deliver_report", {tool.name for tool in app.effect_tools()})
        job_work_tools = {
            tool.name for tool in app._initiative_tools_for_episode(job_work=True)}
        self.assertNotIn("retrieve_report", job_work_tools)
        self.assertNotIn("deliver_report", job_work_tools)
        await app.stop()

    async def test_reference_is_stale_in_next_episode_and_delivery_is_one_effect(self):
        first = ReportBackend(
            {"source_kind": "job_run_result", "selector": f"RUN{self.run.id}"})
        sink = RecordingSink()
        await self.run_episode(first, sink)
        stale = first.results[0]["report_ref"]
        second = ReportBackend(
            {"source_kind": "job_run_result", "selector": "RUN999999"}, forged=stale)
        await self.run_episode(second, sink)
        self.assertEqual(second.results[1]["status"], "rejected")
        self.assertEqual(len(sink.messages), 1)
        delivery = next(tool for tool in first.tool_definitions[1]
                        if tool.name == "deliver_report")
        self.assertEqual(set(delivery.parameters["properties"]),
                         {"report_ref", "destination"})


if __name__ == "__main__":
    unittest.main()

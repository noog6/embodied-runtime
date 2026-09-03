import asyncio
import json
import unittest

from embodied_runtime.app import (
    ADDRESS_OPERATOR_TOOL, ORIENT_BODY_TOOL, ApplicationOptions, RobotApplication,
)
from embodied_runtime.attention import ACTION_INITIATIVE_REQUEST, AttentionStimulus
from embodied_runtime.body.virtual import VirtualBodyBackend
from embodied_runtime.cognition import CognitionError, CognitionToolCall, TextCognitionBackend
from embodied_runtime.console import RuntimeConsole, run_console_session
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.interaction import (
    MAX_OPERATOR_MESSAGE_CHARS, ConsoleOperatorMessageChannel, OperatorMessage,
    OperatorMessageSink,
)
from embodied_runtime.profile import RobotProfile
from embodied_runtime.state import BodyState, LifecycleState
from tests.test_platform import snapshot


class Platform:
    def snapshot(self):
        return snapshot()


class RecordingSink(OperatorMessageSink):
    def __init__(self, fail=False):
        self.messages = []
        self.fail = fail

    async def deliver(self, message):
        if self.fail:
            raise RuntimeError("delivery failed")
        self.messages.append(message)


class ScriptedBackend(TextCognitionBackend):
    identifier = "scripted"

    def __init__(self, calls=(), *, fail_after=False):
        self.calls = calls
        self.fail_after = fail_after
        self.requests = []
        self.results = []

    async def respond(self, message, *, instructions=None, tools=(),
                      tool_executor=None, refreshed_instructions=None):
        self.requests.append((message, instructions, tools, tool_executor,
                              refreshed_instructions))
        for call in self.calls:
            self.results.append(json.loads((await tool_executor(call)).output))
        if self.fail_after:
            raise CognitionError("continuation failed")
        return "done"


class MessageClosureBackend(TextCognitionBackend):
    identifier = "message-closure"

    def __init__(self):
        self.requests = []

    async def respond(self, message, *, instructions=None, tools=(),
                      tool_executor=None, refreshed_instructions=None):
        self.requests.append((message, instructions, tools))
        call = (CognitionToolCall("address_operator", '{"message":"Reflex noticed."}')
                if len(self.requests) == 1 else CognitionToolCall("complete_goal", "{}"))
        await tool_executor(call)
        return "done"


class InteractionTests(unittest.IsolatedAsyncioTestCase):
    def make_app(self, backend=None, sink=None, *, actions=False, messages=True,
                 body=None):
        return RobotApplication(
            RobotProfile("test", "Test Robot"), VirtualHardwareBackend(),
            ApplicationOptions(
                initiative_enabled=True, initiative_actions_enabled=actions,
                initiative_messages_enabled=messages,
            ), platform_provider=Platform(), body_backend=body or VirtualBodyBackend(),
            cognition_backend=backend, operator_message_sink=sink,
        )

    async def test_projection_schema_and_physical_independence(self):
        class PhysicalBody(VirtualBodyBackend):
            is_physical = True

        sink = RecordingSink()
        app = self.make_app(ScriptedBackend(), sink, actions=True,
                            body=PhysicalBody())
        await app.start()
        app.set_goal("goal")
        self.assertEqual(app.initiative_tools(), (ADDRESS_OPERATOR_TOOL,))
        self.assertEqual(ADDRESS_OPERATOR_TOOL.parameters, {
            "type": "object",
            "properties": {"message": {
                "type": "string", "maxLength": MAX_OPERATOR_MESSAGE_CHARS,
            }},
            "required": ["message"], "additionalProperties": False,
        })
        self.assertNotIn("source", ADDRESS_OPERATOR_TOOL.parameters["properties"])
        await app.stop()

    async def test_delivery_normalizes_and_does_not_mutate_state_or_memory(self):
        sink = RecordingSink()
        backend = ScriptedBackend((CognitionToolCall(
            "address_operator", '{"message":"  I noticed the reflex.  "}'
        ),))
        app = self.make_app(backend, sink)
        await app.start()
        goal = app.set_goal("goal")
        state = app.runtime_state
        memory = app.working_memory.snapshot()
        outcome = await app._request_initiative(
            AttentionStimulus("body_orientation_changed", "reflex:test", 1, 0, 0, 0)
        )
        self.assertEqual(backend.requests[0][0], ACTION_INITIATIVE_REQUEST)
        self.assertEqual([tool.name for tool in backend.requests[0][2]],
                         ["address_operator"])
        self.assertEqual((sink.messages[0].text, sink.messages[0].source),
                         ("I noticed the reflex.", "initiative"))
        self.assertEqual(backend.results[0], {
            "message": "I noticed the reflex.", "recipient": "operator",
            "status": "applied",
        })
        self.assertEqual((outcome.action, outcome.action_status),
                         ("address_operator", "applied"))
        self.assertIs(app.active_goal, goal)
        self.assertIs(app.runtime_state, state)
        self.assertEqual(app.working_memory.snapshot(), memory)
        await app.stop()

    async def test_invalid_messages_and_channel_failure_are_rejected(self):
        invalid = (
            '{}', '{"message":1}', '{"message":""}', '{"message":"   "}',
            json.dumps({"message": "x" * (MAX_OPERATOR_MESSAGE_CHARS + 1)}),
            json.dumps({"message": "bad\u001btext"}),
            '{"message":"ok","channel":"console"}',
        )
        sink = RecordingSink()
        app = self.make_app(ScriptedBackend(), sink)
        await app.start()
        app.set_goal("goal")
        for arguments in invalid:
            result = await app._execute_initiative_tool(
                CognitionToolCall("address_operator", arguments)
            )
            self.assertEqual(json.loads(result.output)["status"], "rejected")
        self.assertEqual(sink.messages, [])
        await app.stop()

        failing = RecordingSink(fail=True)
        app = self.make_app(ScriptedBackend(), failing)
        await app.start()
        app.set_goal("goal")
        result = await app._execute_initiative_tool(CognitionToolCall(
            "address_operator", '{"message":"hello"}'
        ))
        self.assertEqual(json.loads(result.output)["status"], "rejected")
        self.assertEqual(app.state, LifecycleState.RUNNING)
        await app.stop()

    async def test_one_capability_total_in_both_orders(self):
        for calls, expected_body, message_count in (
            ((CognitionToolCall("orient_body", '{"yaw_degrees":2,"pitch_degrees":0}'),
              CognitionToolCall("address_operator", '{"message":"hello"}')),
             BodyState(2.0, 0.0), 0),
            ((CognitionToolCall("address_operator", '{"message":"hello"}'),
              CognitionToolCall("orient_body", '{"yaw_degrees":2,"pitch_degrees":0}')),
             BodyState(0.0, 0.0), 1),
        ):
            with self.subTest(calls=calls):
                sink = RecordingSink()
                backend = ScriptedBackend(calls)
                app = self.make_app(backend, sink, actions=True)
                await app.start()
                app.set_goal("goal")
                await app._request_initiative(AttentionStimulus(
                    "body_orientation_changed", "reflex:test", 1, 0, 0, 0
                ))
                self.assertEqual(backend.results[0]["status"], "applied")
                self.assertEqual(backend.results[1]["status"], "rejected")
                self.assertEqual(app.runtime_state.body, expected_body)
                self.assertEqual(len(sink.messages), message_count)
                await app.stop()

    async def test_rejected_first_request_consumes_capability_budget(self):
        sink = RecordingSink()
        backend = ScriptedBackend((
            CognitionToolCall("address_operator", '{"message":""}'),
            CognitionToolCall(
                "orient_body", '{"yaw_degrees":2,"pitch_degrees":0}'
            ),
        ))
        app = self.make_app(backend, sink, actions=True)
        await app.start()
        app.set_goal("goal")
        self.assertEqual(
            [tool.name for tool in app.initiative_tools()],
            ["orient_body", "address_operator"],
        )

        await app._request_initiative(AttentionStimulus(
            "body_orientation_changed", "reflex:test", 1, 0, 0, 0
        ))

        self.assertEqual(backend.results[0]["status"], "rejected")
        self.assertEqual(backend.results[1]["status"], "rejected")
        self.assertEqual(sink.messages, [])
        self.assertEqual(app.runtime_state.body, BodyState(0.0, 0.0))
        await app.stop()

    async def test_provider_failure_preserves_delivered_effect(self):
        sink = RecordingSink()
        backend = ScriptedBackend((CognitionToolCall(
            "address_operator", '{"message":"Question?"}'
        ),), fail_after=True)
        app = self.make_app(backend, sink)
        await app.start()
        app.set_goal("goal")
        with self.assertRaises(CognitionError):
            await app._request_initiative(AttentionStimulus(
                "body_orientation_changed", "reflex:test", 1, 0, 0, 0
            ))
        self.assertEqual(len(sink.messages), 1)
        status = app.attention.status()
        self.assertEqual((status.last_action, status.last_action_status),
                         ("address_operator", "applied"))
        self.assertEqual(len(app.working_memory), 0)
        await app.stop()

    async def test_no_channel_means_no_projection_and_direct_rejection(self):
        app = self.make_app(ScriptedBackend(), None)
        await app.start()
        app.set_goal("goal")
        self.assertEqual(app.initiative_tools(), ())
        result = await app._execute_initiative_tool(CognitionToolCall(
            "address_operator", '{"message":"hello"}'
        ))
        self.assertEqual(json.loads(result.output)["status"], "rejected")
        await app.stop()

    async def test_address_operator_direct_execution_requires_active_goal(self):
        sink = RecordingSink()
        app = self.make_app(ScriptedBackend(), sink)
        await app.start()
        self.assertIsNone(app.active_goal)
        self.assertEqual(app.initiative_tools(), ())

        result = await app._execute_initiative_tool(CognitionToolCall(
            "address_operator", '{"message":"Hello"}'
        ))

        self.assertEqual(json.loads(result.output)["status"], "rejected")
        self.assertEqual(sink.messages, [])
        await app.stop()

    async def test_applied_message_can_complete_same_goal_via_outcome(self):
        sink = RecordingSink()
        backend = MessageClosureBackend()
        app = RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            ApplicationOptions(
                initiative_enabled=True, initiative_messages_enabled=True,
                initiative_goal_closure_enabled=True,
            ), platform_provider=Platform(), body_backend=VirtualBodyBackend(),
            cognition_backend=backend, operator_message_sink=sink,
        )
        await app.start()
        app.set_goal("Tell the operator once, then complete")
        memory = app.working_memory.snapshot()
        await app._request_initiative(AttentionStimulus(
            "body_orientation_changed", "reflex:test", 1, 0, 0, 0
        ))
        self.assertEqual(len(backend.requests), 2)
        self.assertIn("Reflex noticed.", backend.requests[1][1])
        self.assertIn("recipient", backend.requests[1][1])
        self.assertEqual([tool.name for tool in backend.requests[1][2]],
                         ["complete_goal"])
        self.assertIsNone(app.active_goal)
        self.assertEqual(len(sink.messages), 1)
        self.assertEqual(app.working_memory.snapshot(), memory)
        self.assertEqual(app.attention.status().last_goal_closure, "completed")
        await app.stop()


class ConsoleDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_message_is_rendered_while_input_is_idle_and_shutdown_is_clean(self):
        class Terminal:
            def __init__(self):
                self.output = ""
                self.waiting = asyncio.Event()
                self.release = asyncio.Event()

            def write(self, text):
                self.output += text

            async def read_line(self, prompt):
                self.write(prompt)
                self.waiting.set()
                await self.release.wait()
                return None

        channel = ConsoleOperatorMessageChannel()
        app = RobotApplication(
            RobotProfile("mira", "Mira"), VirtualHardwareBackend(),
            platform_provider=Platform(), body_backend=VirtualBodyBackend(),
        )
        await app.start()
        terminal = Terminal()
        session = asyncio.create_task(
            run_console_session(RuntimeConsole(app), terminal, channel)
        )
        await terminal.waiting.wait()
        await channel.deliver(OperatorMessage("Hello.", "initiative"))
        while "Mira: Hello." not in terminal.output:
            await asyncio.sleep(0)
        terminal.release.set()
        await asyncio.wait_for(session, 1)
        self.assertIn("Mira: Hello.", terminal.output)
        await app.stop()

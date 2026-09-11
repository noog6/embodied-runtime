import asyncio
from dataclasses import FrozenInstanceError
import json
import unittest

from embodied_runtime.app import (
    ADDRESS_OPERATOR_TOOL, INSPECT_SELF_TOOL, ORIENT_BODY_TOOL, SCHEDULE_FOLLOWUP_TOOL, ApplicationOptions, RobotApplication,
)
from embodied_runtime.attention import ACTION_INITIATIVE_REQUEST, AttentionStimulus
from embodied_runtime.body.virtual import VirtualBodyBackend
from embodied_runtime.cognition import CognitionError, CognitionToolCall, TextCognitionBackend
from embodied_runtime.console import RuntimeConsole, run_console_session
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.interaction import (
    CONSOLE_ADMINISTRATIVE, CONSOLE_DIALOGUE, CONSOLE_NOTIFICATION,
    VOICE_DIALOGUE, MAX_OPERATOR_MESSAGE_CHARS, ConsoleOperatorMessageChannel,
    InteractionChannel, InteractionContext, InteractionInitiator,
    InteractionMode, OperatorMessage, OperatorMessageSink, runtime_notification,
    render_dialogue_policy, render_notification_context,
    render_notification_policy, resolve_notification_route,
)
from embodied_runtime.profile import RobotProfile
from embodied_runtime.state import BodyState, LifecycleState
from tests.test_platform import snapshot


class Platform:
    def snapshot(self):
        return snapshot()


class InteractionIdentityTests(unittest.TestCase):
    def test_context_is_immutable(self):
        with self.assertRaises(FrozenInstanceError):
            CONSOLE_DIALOGUE.response_expected = False

    def test_canonical_contexts(self):
        self.assertEqual(CONSOLE_DIALOGUE, InteractionContext(
            InteractionChannel.CONSOLE, InteractionMode.DIALOGUE,
            InteractionInitiator.OPERATOR, True,
        ))
        self.assertEqual(VOICE_DIALOGUE, InteractionContext(
            InteractionChannel.VOICE, InteractionMode.DIALOGUE,
            InteractionInitiator.OPERATOR, True,
        ))
        self.assertEqual(CONSOLE_NOTIFICATION, InteractionContext(
            InteractionChannel.CONSOLE, InteractionMode.NOTIFICATION,
            InteractionInitiator.RUNTIME, False,
        ))
        self.assertEqual(CONSOLE_ADMINISTRATIVE, InteractionContext(
            InteractionChannel.CONSOLE, InteractionMode.ADMINISTRATIVE,
            InteractionInitiator.OPERATOR, False,
        ))

    def test_console_sink_and_message_keep_channel_separate_from_provenance(self):
        sink = ConsoleOperatorMessageChannel()
        message = OperatorMessage("hello", "initiative", CONSOLE_NOTIFICATION)
        self.assertEqual(sink.channel, InteractionChannel.CONSOLE)
        self.assertEqual(message.source, "initiative")
        self.assertEqual(message.interaction, CONSOLE_NOTIFICATION)

    def test_runtime_notification_constructs_canonical_semantics(self):
        self.assertEqual(
            runtime_notification(InteractionChannel.CONSOLE),
            CONSOLE_NOTIFICATION,
        )

    def test_notification_route_policy_allows_console_and_rejects_voice(self):
        route = resolve_notification_route(InteractionChannel.CONSOLE)
        self.assertEqual(route, InteractionContext(
            InteractionChannel.CONSOLE,
            InteractionMode.NOTIFICATION,
            InteractionInitiator.RUNTIME,
            False,
        ))
        self.assertIsNone(resolve_notification_route(InteractionChannel.VOICE))
        # Structural representation and route eligibility are intentionally distinct.
        self.assertEqual(
            runtime_notification(InteractionChannel.VOICE).channel,
            InteractionChannel.VOICE,
        )

    def test_dialogue_context_rendering_is_deterministic(self):
        self.assertEqual(CONSOLE_DIALOGUE.render(), """Interaction context
  channel: console
  mode: dialogue
  initiator: operator
  response_expected: true""")
        self.assertEqual(VOICE_DIALOGUE.render(), """Interaction context
  channel: voice
  mode: dialogue
  initiator: operator
  response_expected: true""")

    def test_voice_dialogue_policy_covers_spoken_presentation(self):
        policy = render_dialogue_policy(VOICE_DIALOGUE)
        self.assertIn("Dialogue policy\n  medium: spoken", policy)
        self.assertIn("natural speech", policy)
        self.assertIn("Do not normally recite raw URLs", policy)
        self.assertIn("Do not rely on Markdown-dependent or visual formatting", policy)
        self.assertIn("explicitly requests", policy)
        self.assertIn("provide it", policy)
        self.assertIn("unless the runtime actually performed that action", policy)

    def test_console_dialogue_policy_covers_text_presentation(self):
        policy = render_dialogue_policy(CONSOLE_DIALOGUE)
        self.assertIn("Dialogue policy\n  medium: text", policy)
        self.assertIn("paragraphs and lists", policy)
        self.assertIn("plain terminal", policy)
        self.assertIn("exact URLs, paths, hashes, identifiers", policy)
        self.assertIn("technical detail", policy)

    def test_dialogue_policy_rejects_non_dialogue_context(self):
        with self.assertRaises(ValueError):
            render_dialogue_policy(CONSOLE_NOTIFICATION)

    def test_console_notification_grounding_is_deterministic_and_bounded(self):
        self.assertEqual(render_notification_context(CONSOLE_NOTIFICATION),
                         """Available operator notification
  channel: console
  mode: notification
  initiator: runtime
  response_expected: false""")
        policy = render_notification_policy(CONSOLE_NOTIFICATION)
        for expected in (
            "Notification policy\n  medium: text", "asynchronously", "self-contained",
            "does not itself open or extend a conversation", "No direct reply is expected",
            "open-ended conversational question", "request for genuine operator action",
            "saw, read, or acknowledged", "local plain-text terminal",
            "do not assume a Markdown renderer",
        ):
            self.assertIn(expected, policy)

    def test_notification_grounding_rejects_non_notification_contexts(self):
        for interaction in (CONSOLE_DIALOGUE, CONSOLE_ADMINISTRATIVE):
            with self.subTest(interaction=interaction), self.assertRaises(ValueError):
                render_notification_context(interaction)
            with self.subTest(interaction=interaction), self.assertRaises(ValueError):
                render_notification_policy(interaction)


class ConsoleChannelValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_accepts_valid_console_notification(self):
        channel = ConsoleOperatorMessageChannel()
        message = OperatorMessage("hello", "initiative", CONSOLE_NOTIFICATION)
        await channel.deliver(message)
        self.assertIs(await channel.receive(), message)

    async def test_rejects_non_notification_semantics_without_enqueueing(self):
        invalid = (
            CONSOLE_DIALOGUE,
            CONSOLE_ADMINISTRATIVE,
            runtime_notification(InteractionChannel.VOICE),
            InteractionContext(
                InteractionChannel.CONSOLE,
                InteractionMode.NOTIFICATION,
                InteractionInitiator.RUNTIME,
                True,
            ),
        )
        channel = ConsoleOperatorMessageChannel()
        for interaction in invalid:
            with self.assertRaises(ValueError):
                await channel.deliver(OperatorMessage(
                    "hello", "initiative", interaction
                ))
        self.assertTrue(channel._messages.empty())


class RecordingSink(OperatorMessageSink):
    @property
    def channel(self):
        return InteractionChannel.CONSOLE

    def __init__(self, fail=False):
        self.messages = []
        self.fail = fail

    async def deliver(self, message):
        if self.fail:
            raise RuntimeError("delivery failed")
        self.messages.append(message)


class VoiceRecordingSink(RecordingSink):
    """Test-only proof that transport presence does not grant route authority."""

    @property
    def channel(self):
        return InteractionChannel.VOICE


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


class AcquisitionThenNotificationBackend(TextCognitionBackend):
    identifier = "acquisition-then-notification"

    def __init__(self):
        self.requests = []
        self.refreshed = []

    async def respond(self, message, *, instructions=None, tools=(),
                      tool_executor=None, refreshed_instructions=None):
        self.requests.append((instructions, tuple(tool.name for tool in tools)))
        if refreshed_instructions is not None:
            self.refreshed.append(refreshed_instructions())
        call = (CognitionToolCall("inspect_self", '{"area":"runtime"}')
                if len(self.requests) == 1 else
                CognitionToolCall("address_operator", '{"message":"Inspection complete."}'))
        await tool_executor(call)
        return "done"


class RouteReplacementBackend(TextCognitionBackend):
    identifier = "route-replacement"

    def __init__(self, replace):
        self.replace = replace
        self.instructions = None
        self.tools = ()
        self.result = None

    async def respond(self, message, *, instructions=None, tools=(),
                      tool_executor=None, refreshed_instructions=None):
        self.instructions = instructions
        self.tools = tools
        self.replace()
        self.result = json.loads((await tool_executor(CognitionToolCall(
            "address_operator", '{"message":"Route-sensitive."}'
        ))).output)
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
        self.assertEqual(app.initiative_tools(), (INSPECT_SELF_TOOL, SCHEDULE_FOLLOWUP_TOOL, ADDRESS_OPERATOR_TOOL,))
        self.assertEqual(ADDRESS_OPERATOR_TOOL.parameters, {
            "type": "object",
            "properties": {"message": {
                "type": "string", "maxLength": MAX_OPERATOR_MESSAGE_CHARS,
            }},
            "required": ["message"], "additionalProperties": False,
        })
        self.assertNotIn("source", ADDRESS_OPERATOR_TOOL.parameters["properties"])
        for forbidden in ("channel", "route", "transport", "medium", "destination"):
            self.assertNotIn(forbidden, ADDRESS_OPERATOR_TOOL.parameters["properties"])
        await app.stop()

    async def test_voice_sink_does_not_project_or_ground_notifications(self):
        sink = VoiceRecordingSink()
        backend = ScriptedBackend()
        app = self.make_app(backend, sink)
        await app.start()
        app.set_goal("goal")

        self.assertNotIn("address_operator", tuple(
            tool.name for tool in app.initiative_tools()
        ))
        await app._request_initiative(AttentionStimulus(
            "body_orientation_changed", "reflex:test", 1, 0, 0, 0
        ))
        instructions = backend.requests[0][1]
        self.assertNotIn("Available operator notification", instructions)
        self.assertNotIn("Notification policy", instructions)
        self.assertEqual(sink.messages, [])
        self.assertIn("channel: voice", VOICE_DIALOGUE.render())
        self.assertIn("Dialogue policy", render_dialogue_policy(VOICE_DIALOGUE))
        await app.stop()

    async def test_delivery_rejects_route_change_without_state_or_memory_effects(self):
        initial = RecordingSink()
        replacement = VoiceRecordingSink()
        app = None
        backend = RouteReplacementBackend(
            lambda: setattr(app, "_operator_message_sink", replacement)
        )
        app = self.make_app(backend, initial)
        await app.start()
        app.set_goal("goal")
        state = app.runtime_state
        memory = app.working_memory.snapshot()

        outcome = await app._request_initiative(AttentionStimulus(
            "body_orientation_changed", "reflex:test", 1, 0, 0, 0
        ))

        self.assertIn("channel: console", backend.instructions)
        self.assertIn("address_operator", tuple(tool.name for tool in backend.tools))
        self.assertEqual(backend.result["status"], "rejected")
        self.assertEqual(outcome.action_status, "rejected")
        self.assertEqual(initial.messages, [])
        self.assertEqual(replacement.messages, [])
        self.assertIs(app.runtime_state, state)
        self.assertEqual(app.working_memory.snapshot(), memory)
        self.assertIsNone(app.episode_coordinator.current)
        await app.stop()

    async def test_delivery_uses_current_same_channel_replacement(self):
        initial = RecordingSink()
        replacement = RecordingSink()
        app = None
        backend = RouteReplacementBackend(
            lambda: setattr(app, "_operator_message_sink", replacement)
        )
        app = self.make_app(backend, initial)
        await app.start()
        app.set_goal("goal")

        outcome = await app._request_initiative(AttentionStimulus(
            "body_orientation_changed", "reflex:test", 1, 0, 0, 0
        ))

        self.assertEqual(backend.result["status"], "applied")
        self.assertEqual(outcome.action_status, "applied")
        self.assertEqual(initial.messages, [])
        self.assertEqual(len(replacement.messages), 1)
        self.assertEqual(replacement.messages[0].interaction, CONSOLE_NOTIFICATION)
        self.assertEqual(replacement.messages[0].source, "initiative")
        await app.stop()

    async def test_invalid_explicit_operator_contexts_are_rejected_before_effects(self):
        backend = ScriptedBackend()
        app = self.make_app(backend)
        await app.start()
        before = app.working_memory.snapshot()
        invalid = (
            CONSOLE_NOTIFICATION,
            CONSOLE_ADMINISTRATIVE,
            InteractionContext(
                InteractionChannel.VOICE, InteractionMode.DIALOGUE,
                InteractionInitiator.RUNTIME, True,
            ),
            InteractionContext(
                InteractionChannel.VOICE, InteractionMode.DIALOGUE,
                InteractionInitiator.OPERATOR, False,
            ),
        )
        for interaction in invalid:
            with self.subTest(interaction=interaction), self.assertRaises(ValueError):
                await app.request_cognition("hello", interaction=interaction)
        self.assertEqual(backend.requests, [])
        self.assertIsNone(app.episode_coordinator.current)
        self.assertIsNone(app.episode_coordinator.last)
        self.assertEqual(app.working_memory.snapshot(), before)
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
        instructions = backend.requests[0][1]
        self.assertEqual(instructions.count("Available operator notification"), 1)
        self.assertEqual(instructions.count("Notification policy"), 1)
        self.assertEqual([tool.name for tool in backend.requests[0][2]],
                         ["inspect_self", "schedule_followup", "address_operator"])
        self.assertEqual((sink.messages[0].text, sink.messages[0].source),
                         ("I noticed the reflex.", "initiative"))
        self.assertEqual(sink.messages[0].interaction, CONSOLE_NOTIFICATION)
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

    async def test_notification_grounding_follows_projected_tool_availability(self):
        for sink, messages in ((RecordingSink(), False), (None, True)):
            with self.subTest(sink=sink, messages=messages):
                backend = ScriptedBackend()
                app = self.make_app(backend, sink, messages=messages)
                await app.start()
                app.set_goal("goal")
                tools_before = tuple(tool.name for tool in app.initiative_tools())
                await app._request_initiative(AttentionStimulus(
                    "body_orientation_changed", "reflex:test", 1, 0, 0, 0
                ))
                instructions = backend.requests[0][1]
                self.assertNotIn("Available operator notification", instructions)
                self.assertNotIn("Notification policy", instructions)
                self.assertNotIn("address_operator", tools_before)
                self.assertEqual(tuple(tool.name for tool in backend.requests[0][2]),
                                 tools_before)
                await app.stop()

    async def test_notification_identity_is_stable_across_acquisition_stages(self):
        sink = RecordingSink()
        backend = AcquisitionThenNotificationBackend()
        app = self.make_app(backend, sink)
        await app.start()
        app.set_goal("inspect then notify")
        tools_before = tuple(tool.name for tool in app.initiative_tools())

        await app._request_initiative(AttentionStimulus(
            "body_orientation_changed", "reflex:test", 1, 0, 0, 0
        ))

        self.assertEqual(len(backend.requests), 2)
        self.assertEqual(len(backend.refreshed), 2)
        for instructions in (
            backend.requests[0][0], backend.refreshed[0],
            backend.requests[1][0], backend.refreshed[1],
        ):
            self.assertEqual(instructions.count("Available operator notification"), 1)
            self.assertEqual(instructions.count("Notification policy"), 1)
            self.assertIn("channel: console", instructions)
            self.assertIn("mode: notification", instructions)
            self.assertIn("initiator: runtime", instructions)
            self.assertIn("response_expected: false", instructions)
            self.assertIn("id: E0", instructions)
        self.assertEqual(backend.requests[0][1], tools_before)
        self.assertEqual(backend.requests[1][1], (
            "inspect_self", "schedule_followup", "address_operator",
        ))
        self.assertEqual(len(sink.messages), 1)
        self.assertEqual(sink.messages[0].interaction, CONSOLE_NOTIFICATION)
        self.assertEqual(sink.messages[0].source, "initiative")
        self.assertEqual(len(app.working_memory), 0)
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
            ["inspect_self", "schedule_followup", "orient_body", "address_operator"],
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
        self.assertEqual(app.initiative_tools(), (INSPECT_SELF_TOOL, SCHEDULE_FOLLOWUP_TOOL,))
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
        await channel.deliver(OperatorMessage(
            "Hello.", "initiative", CONSOLE_NOTIFICATION
        ))
        while "Mira: Hello." not in terminal.output:
            await asyncio.sleep(0)
        terminal.release.set()
        await asyncio.wait_for(session, 1)
        self.assertIn("Mira: Hello.", terminal.output)
        await app.stop()

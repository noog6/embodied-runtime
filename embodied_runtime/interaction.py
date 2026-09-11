"""Provider-neutral delivery boundary for messages addressed to the operator."""

from abc import ABC, abstractmethod
import asyncio
from dataclasses import dataclass
from enum import StrEnum


MAX_OPERATOR_MESSAGE_CHARS = 1000


class InteractionChannel(StrEnum):
    CONSOLE = "console"
    VOICE = "voice"


class InteractionMode(StrEnum):
    DIALOGUE = "dialogue"
    NOTIFICATION = "notification"
    ADMINISTRATIVE = "administrative"


class InteractionInitiator(StrEnum):
    OPERATOR = "operator"
    RUNTIME = "runtime"


@dataclass(frozen=True, slots=True)
class InteractionContext:
    """Provider-neutral identity of one communication interaction."""

    channel: InteractionChannel
    mode: InteractionMode
    initiator: InteractionInitiator
    response_expected: bool

    def render(self) -> str:
        """Render bounded, provider-neutral grounding for this interaction."""
        return "\n".join((
            "Interaction context",
            f"  channel: {self.channel.value}",
            f"  mode: {self.mode.value}",
            f"  initiator: {self.initiator.value}",
            f"  response_expected: {str(self.response_expected).lower()}",
        ))


def render_dialogue_policy(interaction: InteractionContext) -> str:
    """Render presentation guidance for one supported operator dialogue turn."""
    if not (
        interaction.mode == InteractionMode.DIALOGUE
        and interaction.initiator == InteractionInitiator.OPERATOR
        and interaction.response_expected is True
    ):
        raise ValueError("dialogue policy requires operator dialogue with a response expected")
    if interaction.channel == InteractionChannel.VOICE:
        return "\n".join((
            "Dialogue policy",
            "  medium: spoken",
            "The final response will be spoken aloud. Write for natural speech, not a screen.",
            "Prefer concise conversational sentences, while giving enough information to answer the request; do not become artificially terse.",
            "Do not rely on Markdown-dependent or visual formatting, and avoid tables and visual-layout references such as 'see below', 'the table above', 'click this', or 'here is the link'.",
            "Do not normally recite raw URLs or verbalize Markdown punctuation or formatting syntax.",
            "Avoid dumping long code, file paths, identifiers, hashes, serialized data, or other visually parsed strings unless the operator explicitly asks for the exact value.",
            "When exact textual material is important but was not explicitly requested, describe it naturally rather than reading punctuation-heavy content aloud.",
            "If the operator explicitly requests an exact URL, path, hash, identifier, spelling, or other exact text, provide it; this presentation preference does not censor requested data.",
            "Never claim text, a link, or code was displayed, sent, copied, or delivered on another channel unless the runtime actually performed that action.",
        ))
    if interaction.channel == InteractionChannel.CONSOLE:
        return "\n".join((
            "Dialogue policy",
            "  medium: text",
            "The final response will be displayed as text in a local plain terminal.",
            "Use readable text-native structure, including paragraphs and lists when useful, without assuming a Markdown renderer.",
            "Include exact URLs, paths, hashes, identifiers, command lines, and code-like text when useful.",
            "Provide technical detail when appropriate to the request, but verbosity is not required.",
        ))
    raise ValueError(f"unsupported operator dialogue channel: {interaction.channel}")


def _require_console_notification(interaction: InteractionContext) -> None:
    if not (
        interaction.channel == InteractionChannel.CONSOLE
        and interaction.mode == InteractionMode.NOTIFICATION
        and interaction.initiator == InteractionInitiator.RUNTIME
        and interaction.response_expected is False
    ):
        raise ValueError("notification grounding requires a console runtime notification")


def render_notification_context(interaction: InteractionContext) -> str:
    """Render the destination of an available outbound notification effect."""
    _require_console_notification(interaction)
    return "\n".join((
        "Available operator notification",
        f"  channel: {interaction.channel.value}",
        f"  mode: {interaction.mode.value}",
        f"  initiator: {interaction.initiator.value}",
        f"  response_expected: {str(interaction.response_expected).lower()}",
    ))


def render_notification_policy(interaction: InteractionContext) -> str:
    """Render bounded composition guidance for a console notification."""
    _require_console_notification(interaction)
    return "\n".join((
        "Notification policy",
        "  medium: text",
        "The message will be delivered asynchronously as a runtime-originated notification in a local plain-text terminal.",
        "It is self-contained, is not an operator dialogue response, and does not itself open or extend a conversation or reserve cognition waiting for a reply.",
        "No direct reply is expected as part of this notification, but the operator may respond later in a new operator cognition episode.",
        "Make the message stand on its own, with enough context to explain why it matters; prefer concise useful information over narration of internal reasoning.",
        "Do not append a casual or open-ended conversational question merely to continue conversation, or imply that the runtime is waiting for an answer.",
        "A direct request for genuine operator action, such as asking the operator to connect power, is permitted when the situation calls for it.",
        "Do not claim delivery to another channel, or claim the operator saw, read, or acknowledged the message, or that a reply was received, without runtime evidence.",
        "Compose plain terminal text and do not assume a Markdown renderer.",
    ))


def runtime_notification(channel: InteractionChannel) -> InteractionContext:
    """Construct the runtime notification identity for a delivery channel."""
    return InteractionContext(
        channel=channel,
        mode=InteractionMode.NOTIFICATION,
        initiator=InteractionInitiator.RUNTIME,
        response_expected=False,
    )


def resolve_notification_route(
    channel: InteractionChannel,
) -> InteractionContext | None:
    """Resolve an eligible autonomous operator-notification route.

    Notification-shaped interactions can be constructed for any known channel;
    this policy boundary separately determines which channels the runtime is
    currently willing and able to offer for autonomous delivery.
    """
    if channel == InteractionChannel.CONSOLE:
        return runtime_notification(channel)
    return None


CONSOLE_DIALOGUE = InteractionContext(
    InteractionChannel.CONSOLE, InteractionMode.DIALOGUE,
    InteractionInitiator.OPERATOR, True,
)
VOICE_DIALOGUE = InteractionContext(
    InteractionChannel.VOICE, InteractionMode.DIALOGUE,
    InteractionInitiator.OPERATOR, True,
)
CONSOLE_NOTIFICATION = runtime_notification(InteractionChannel.CONSOLE)
CONSOLE_ADMINISTRATIVE = InteractionContext(
    InteractionChannel.CONSOLE, InteractionMode.ADMINISTRATIVE,
    InteractionInitiator.OPERATOR, False,
)


@dataclass(frozen=True, slots=True)
class OperatorMessage:
    """One bounded, runtime-sourced plain-text message for the operator."""

    text: str
    source: str
    interaction: InteractionContext


class OperatorMessageSink(ABC):
    """Accept an operator message for delivery by an application-owned channel."""

    @property
    @abstractmethod
    def channel(self) -> InteractionChannel:
        """Return the channel on which this sink delivers messages."""

    @abstractmethod
    async def deliver(self, message: OperatorMessage) -> None:
        """Accept or deliver *message*, raising when delivery fails."""


class ConsoleOperatorMessageChannel(OperatorMessageSink):
    """Transient queue joining runtime delivery to the local console."""

    def __init__(self) -> None:
        self._messages: asyncio.Queue[OperatorMessage] = asyncio.Queue()

    @property
    def channel(self) -> InteractionChannel:
        return InteractionChannel.CONSOLE

    async def deliver(self, message: OperatorMessage) -> None:
        interaction = message.interaction
        if not (
            interaction.channel == self.channel
            and interaction.mode == InteractionMode.NOTIFICATION
            and interaction.initiator == InteractionInitiator.RUNTIME
            and interaction.response_expected is False
        ):
            raise ValueError("console channel accepts only runtime notifications")
        await self._messages.put(message)

    async def receive(self) -> OperatorMessage:
        return await self._messages.get()

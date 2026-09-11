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


def runtime_notification(channel: InteractionChannel) -> InteractionContext:
    """Construct the runtime notification identity for a delivery channel."""
    return InteractionContext(
        channel=channel,
        mode=InteractionMode.NOTIFICATION,
        initiator=InteractionInitiator.RUNTIME,
        response_expected=False,
    )


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

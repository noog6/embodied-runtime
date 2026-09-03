"""Provider-neutral delivery boundary for messages addressed to the operator."""

from abc import ABC, abstractmethod
import asyncio
from dataclasses import dataclass


MAX_OPERATOR_MESSAGE_CHARS = 1000


@dataclass(frozen=True, slots=True)
class OperatorMessage:
    """One bounded, runtime-sourced plain-text message for the operator."""

    text: str
    source: str


class OperatorMessageSink(ABC):
    """Accept an operator message for delivery by an application-owned channel."""

    @abstractmethod
    async def deliver(self, message: OperatorMessage) -> None:
        """Accept or deliver *message*, raising when delivery fails."""


class ConsoleOperatorMessageChannel(OperatorMessageSink):
    """Transient queue joining runtime delivery to the local console."""

    def __init__(self) -> None:
        self._messages: asyncio.Queue[OperatorMessage] = asyncio.Queue()

    async def deliver(self, message: OperatorMessage) -> None:
        await self._messages.put(message)

    async def receive(self) -> OperatorMessage:
        return await self._messages.get()

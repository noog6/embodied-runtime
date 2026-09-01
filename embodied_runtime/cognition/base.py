"""Minimal text cognition contract."""

from abc import ABC, abstractmethod


class CognitionError(RuntimeError):
    """A text cognition request could not be completed."""


class CognitionUnavailableError(CognitionError):
    """The configured cognition backend is unavailable."""


class TextCognitionBackend(ABC):
    """One independent text request in, one text response out."""

    identifier: str

    @abstractmethod
    async def respond(
        self, message: str, *, instructions: str | None = None
    ) -> str:
        """Return the text response to one independent request."""

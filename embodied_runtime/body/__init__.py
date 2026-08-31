"""Semantic robot-body backends."""

from embodied_runtime.body.base import BodyBackend
from embodied_runtime.body.virtual import VirtualBodyBackend

__all__ = ["BodyBackend", "VirtualBodyBackend"]

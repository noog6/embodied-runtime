"""Reusable runtime for embodied agents."""

from importlib import import_module


def __getattr__(name):
    """Preserve public imports without initializing the runtime in console clients."""
    modules = {
        "ApplicationOptions": "app", "RobotApplication": "app",
        "RobotProfile": "profile", "load_profile": "profile",
        "BodyState": "state", "LifecycleState": "state",
        "PresenceState": "state", "RuntimeState": "state",
    }
    if name not in modules:
        raise AttributeError(name)
    value = getattr(import_module(f"embodied_runtime.{modules[name]}"), name)
    globals()[name] = value
    return value

__all__ = [
    "ApplicationOptions",
    "BodyState",
    "LifecycleState",
    "RobotApplication",
    "PresenceState",
    "RuntimeState",
    "RobotProfile",
    "load_profile",
]

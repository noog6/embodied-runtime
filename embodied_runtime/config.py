"""Strict startup configuration loading and CLI/default resolution."""

from dataclasses import dataclass
from pathlib import Path
import tomllib


class ConfigurationError(ValueError):
    """Raised for an operator-facing runtime configuration error."""


@dataclass(frozen=True)
class RuntimeFileConfig:
    profile: str | None = None
    hardware: str | None = None
    camera: str | None = None
    cognition: str | None = None
    mode: str | None = None


@dataclass(frozen=True)
class InitiativeFileConfig:
    enabled: bool | None = None
    platform_attention: bool | None = None
    actions: bool | None = None
    messages: bool | None = None
    continuation: bool | None = None
    goal_closure: bool | None = None


@dataclass(frozen=True)
class RuntimeFileConfiguration:
    runtime: RuntimeFileConfig = RuntimeFileConfig()
    initiative: InitiativeFileConfig = InitiativeFileConfig()


@dataclass(frozen=True)
class LaunchConfiguration:
    """The small set of runtime-significant values configurable at launch."""

    profile: str
    hardware: str
    camera: str
    cognition: str
    mode: str
    initiative: bool
    initiative_platform_attention: bool
    initiative_actions: bool
    initiative_messages: bool
    initiative_continuation: bool
    initiative_goal_closure: bool


HISTORICAL_DEFAULTS = LaunchConfiguration(
    profile="mira", hardware="virtual", camera="none", cognition="none", mode="run",
    initiative=False, initiative_platform_attention=False,
    initiative_actions=False, initiative_messages=False,
    initiative_continuation=False, initiative_goal_closure=False,
)

_RUNTIME_KEYS = {"profile", "hardware", "camera", "cognition", "mode"}
_INITIATIVE_KEYS = {
    "enabled", "platform_attention", "actions", "messages", "continuation",
    "goal_closure",
}
_ENUMS = {
    "runtime.hardware": {"virtual", "fusion-hat"},
    "runtime.camera": {"none", "picamera2"},
    "runtime.cognition": {"none", "openai-responses"},
    "runtime.mode": {"run", "console", "diagnostics"},
}


def load_runtime_config(path: Path) -> RuntimeFileConfiguration:
    """Load one strict TOML file without applying cross-field dependencies."""
    try:
        if not path.is_file():
            raise ConfigurationError(f"configuration file not found: {path}")
        with path.open("rb") as config_file:
            data = tomllib.load(config_file)
    except ConfigurationError:
        raise
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigurationError(f"invalid configuration {path}: {error}") from error

    if not isinstance(data, dict):
        raise ConfigurationError(f"invalid configuration {path}: expected a TOML table")
    _reject_unknown(data, {"runtime", "initiative"})
    runtime = _table(data, "runtime")
    initiative = _table(data, "initiative")
    _reject_unknown(runtime, _RUNTIME_KEYS, "runtime")
    _reject_unknown(initiative, _INITIATIVE_KEYS, "initiative")

    for key, value in runtime.items():
        name = f"runtime.{key}"
        if not isinstance(value, str):
            raise ConfigurationError(f"{name} must be a string")
        choices = _ENUMS.get(name)
        if choices is not None and value not in choices:
            raise ConfigurationError(
                f"unsupported value for {name}: {value!r} "
                f"(choose from {', '.join(sorted(choices))})"
            )
    for key, value in initiative.items():
        if not isinstance(value, bool):
            raise ConfigurationError(f"initiative.{key} must be boolean")

    return RuntimeFileConfiguration(
        RuntimeFileConfig(**runtime), InitiativeFileConfig(**initiative)
    )


def resolve_launch_configuration(
    cli_values: object, file_config: RuntimeFileConfiguration | None = None
) -> LaunchConfiguration:
    """Merge explicit CLI values over file values and historical defaults."""
    file_config = file_config or RuntimeFileConfiguration()
    runtime = file_config.runtime
    initiative = file_config.initiative

    def scalar(name: str, configured: object, historical: object) -> object:
        explicit = getattr(cli_values, name, None)
        return explicit if explicit is not None else (
            configured if configured is not None else historical
        )

    cli_mode = "console" if getattr(cli_values, "console", None) else (
        "diagnostics" if getattr(cli_values, "diagnostics", None) else None
    )
    mode = cli_mode or runtime.mode or HISTORICAL_DEFAULTS.mode

    def opt_in(name: str, configured: bool | None, historical: bool) -> bool:
        explicit = getattr(cli_values, name, None)
        return True if explicit is True else (
            configured if configured is not None else historical
        )

    return LaunchConfiguration(
        profile=scalar("profile", runtime.profile, HISTORICAL_DEFAULTS.profile),
        hardware=scalar("hardware", runtime.hardware, HISTORICAL_DEFAULTS.hardware),
        camera=scalar("camera", runtime.camera, HISTORICAL_DEFAULTS.camera),
        cognition=scalar("cognition", runtime.cognition, HISTORICAL_DEFAULTS.cognition),
        mode=mode,
        initiative=opt_in("initiative", initiative.enabled, False),
        initiative_platform_attention=opt_in(
            "initiative_platform_attention", initiative.platform_attention, False
        ),
        initiative_actions=opt_in("initiative_actions", initiative.actions, False),
        initiative_messages=opt_in("initiative_messages", initiative.messages, False),
        initiative_continuation=opt_in(
            "initiative_continuation", initiative.continuation, False
        ),
        initiative_goal_closure=opt_in(
            "initiative_goal_closure", initiative.goal_closure, False
        ),
    )


def _table(data: dict[str, object], name: str) -> dict[str, object]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigurationError(f"configuration section {name} must be a table")
    return value


def _reject_unknown(
    values: dict[str, object], allowed: set[str], prefix: str | None = None
) -> None:
    unknown = values.keys() - allowed
    if unknown:
        key = sorted(unknown)[0]
        qualified = f"{prefix}.{key}" if prefix else key
        raise ConfigurationError(f"unknown configuration key: {qualified}")

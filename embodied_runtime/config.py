"""Strict startup configuration loading and CLI/default resolution."""

from dataclasses import dataclass
import math
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
    vision: str | None = None
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
class VoiceFileConfig:
    enabled: bool | None = None
    wake_word_enabled: bool | None = None
    wake_words: list[str] | None = None
    tts: str | None = None
    piper_model: str | None = None
    openai_tts_model: str | None = None
    openai_tts_voice: str | None = None
    elevenlabs_tts_model: str | None = None
    elevenlabs_tts_voice_id: str | None = None
    elevenlabs_tts_speed: float | None = None
    initial_timeout_seconds: float | None = None
    followup_timeout_seconds: float | None = None


@dataclass(frozen=True)
class RuntimeFileConfiguration:
    runtime: RuntimeFileConfig = RuntimeFileConfig()
    initiative: InitiativeFileConfig = InitiativeFileConfig()
    voice: VoiceFileConfig = VoiceFileConfig()


@dataclass(frozen=True)
class LaunchConfiguration:
    """The small set of runtime-significant values configurable at launch."""

    profile: str
    hardware: str
    camera: str
    cognition: str
    vision: str
    mode: str
    initiative: bool
    initiative_platform_attention: bool
    initiative_actions: bool
    initiative_messages: bool
    initiative_continuation: bool
    initiative_goal_closure: bool
    voice_enabled: bool
    voice_wake_word_enabled: bool
    voice_wake_words: list[str]
    voice_tts: str
    voice_piper_model: str | None
    voice_openai_tts_model: str
    voice_openai_tts_voice: str
    voice_elevenlabs_tts_model: str
    voice_elevenlabs_tts_voice_id: str | None
    voice_elevenlabs_tts_speed: float
    voice_initial_timeout_seconds: float
    voice_followup_timeout_seconds: float


HISTORICAL_DEFAULTS = LaunchConfiguration(
    profile="mira", hardware="virtual", camera="none", cognition="none", vision="none", mode="run",
    initiative=False, initiative_platform_attention=False,
    initiative_actions=False, initiative_messages=False,
    initiative_continuation=False, initiative_goal_closure=False,
    voice_enabled=False, voice_wake_word_enabled=False, voice_wake_words=["mira"],
    voice_tts="espeak", voice_piper_model=None,
    voice_openai_tts_model="gpt-4o-mini-tts", voice_openai_tts_voice="cedar",
    voice_elevenlabs_tts_model="eleven_flash_v2_5",
    voice_elevenlabs_tts_voice_id=None,
    voice_elevenlabs_tts_speed=1.0,
    voice_initial_timeout_seconds=18.0,
    voice_followup_timeout_seconds=10.0,
)

_RUNTIME_KEYS = {"profile", "hardware", "camera", "cognition", "vision", "mode"}
_INITIATIVE_KEYS = {
    "enabled", "platform_attention", "actions", "messages", "continuation",
    "goal_closure",
}
_VOICE_KEYS = {
    "enabled", "wake_word_enabled", "wake_words", "initial_timeout_seconds",
    "followup_timeout_seconds", "tts", "piper_model", "openai_tts_model",
    "openai_tts_voice",
    "elevenlabs_tts_model", "elevenlabs_tts_voice_id", "elevenlabs_tts_speed",
}
_ENUMS = {
    "runtime.hardware": {"virtual", "fusion-hat"},
    "runtime.camera": {"none", "picamera2"},
    "runtime.cognition": {"none", "openai-responses"},
    "runtime.vision": {"none", "openai-responses"},
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
    _reject_unknown(data, {"runtime", "initiative", "voice"})
    runtime = _table(data, "runtime")
    initiative = _table(data, "initiative")
    voice = _table(data, "voice")
    _reject_unknown(runtime, _RUNTIME_KEYS, "runtime")
    _reject_unknown(initiative, _INITIATIVE_KEYS, "initiative")
    _reject_unknown(voice, _VOICE_KEYS, "voice")

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
    if "enabled" in voice and not isinstance(voice["enabled"], bool):
        raise ConfigurationError("voice.enabled must be boolean")
    if "wake_word_enabled" in voice and not isinstance(
        voice["wake_word_enabled"], bool
    ):
        raise ConfigurationError("voice.wake_word_enabled must be boolean")
    if "wake_words" in voice:
        wake_words = voice["wake_words"]
        if not isinstance(wake_words, list) or not wake_words:
            raise ConfigurationError("voice.wake_words must be a non-empty list")
        if any(
            not isinstance(wake_word, str) or not wake_word.strip()
            for wake_word in wake_words
        ):
            raise ConfigurationError(
                "voice.wake_words entries must be non-empty strings"
            )
    if "tts" in voice:
        if not isinstance(voice["tts"], str):
            raise ConfigurationError("voice.tts must be a string")
        if voice["tts"] not in {"elevenlabs", "espeak", "openai", "piper"}:
            raise ConfigurationError(
                f"unsupported value for voice.tts: {voice['tts']!r} "
                "(choose from elevenlabs, espeak, openai, piper)"
            )
    if "piper_model" in voice and not isinstance(voice["piper_model"], str):
        raise ConfigurationError("voice.piper_model must be a string")
    for key in ("openai_tts_model", "openai_tts_voice"):
        if key in voice and not isinstance(voice[key], str):
            raise ConfigurationError(f"voice.{key} must be a string")
    for key in ("elevenlabs_tts_model", "elevenlabs_tts_voice_id"):
        if key in voice and not isinstance(voice[key], str):
            raise ConfigurationError(f"voice.{key} must be a string")
    if "elevenlabs_tts_speed" in voice:
        speed = voice["elevenlabs_tts_speed"]
        if isinstance(speed, bool) or not isinstance(speed, (int, float)):
            raise ConfigurationError(
                "voice.elevenlabs_tts_speed must be a number from 0.7 to 1.2"
            )
        if not math.isfinite(speed) or not 0.7 <= speed <= 1.2:
            raise ConfigurationError(
                "voice.elevenlabs_tts_speed must be from 0.7 to 1.2"
            )
        voice["elevenlabs_tts_speed"] = float(speed)
    for key in ("initial_timeout_seconds", "followup_timeout_seconds"):
        if key in voice and (isinstance(voice[key], bool) or not isinstance(voice[key], (int, float)) or voice[key] <= 0):
            raise ConfigurationError(f"voice.{key} must be a positive number")

    return RuntimeFileConfiguration(
        RuntimeFileConfig(**runtime), InitiativeFileConfig(**initiative), VoiceFileConfig(**voice)
    )


def resolve_launch_configuration(
    cli_values: object, file_config: RuntimeFileConfiguration | None = None
) -> LaunchConfiguration:
    """Merge explicit CLI values over file values and historical defaults."""
    file_config = file_config or RuntimeFileConfiguration()
    runtime = file_config.runtime
    initiative = file_config.initiative
    voice = file_config.voice

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
        vision=scalar("vision", runtime.vision, HISTORICAL_DEFAULTS.vision),
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
        voice_enabled=opt_in("voice", voice.enabled, False),
        voice_wake_word_enabled=voice.wake_word_enabled or False,
        voice_wake_words=voice.wake_words or ["mira"],
        voice_tts=scalar("tts", voice.tts, "espeak"),
        voice_piper_model=scalar("piper_model", voice.piper_model, None),
        voice_openai_tts_model=scalar(
            "openai_tts_model", voice.openai_tts_model, "gpt-4o-mini-tts"
        ),
        voice_openai_tts_voice=scalar(
            "openai_tts_voice", voice.openai_tts_voice, "cedar"
        ),
        voice_elevenlabs_tts_model=scalar(
            "elevenlabs_tts_model", voice.elevenlabs_tts_model,
            "eleven_flash_v2_5",
        ),
        voice_elevenlabs_tts_voice_id=scalar(
            "elevenlabs_tts_voice_id", voice.elevenlabs_tts_voice_id, None
        ),
        voice_elevenlabs_tts_speed=scalar(
            "elevenlabs_tts_speed", voice.elevenlabs_tts_speed, 1.0
        ),
        voice_initial_timeout_seconds=(voice.initial_timeout_seconds if voice.initial_timeout_seconds is not None else 18.0),
        voice_followup_timeout_seconds=(voice.followup_timeout_seconds if voice.followup_timeout_seconds is not None else 10.0),
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

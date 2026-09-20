"""Strict startup configuration loading and CLI/default resolution."""

from dataclasses import dataclass
import math
from pathlib import Path
import tomllib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


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
    timezone: str | None = None


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
class MemoryFileConfig:
    enabled: bool = False
    database_path: Path | None = None


@dataclass(frozen=True)
class JobsFileConfig:
    enabled: bool = False
    database_path: Path | None = None
    auto_continue: bool = False
    heartbeat_seconds: float = 30.0
    max_auto_steps: int = 3
    scheduler_poll_seconds: float = 30.0


@dataclass(frozen=True)
class RuntimeFileConfiguration:
    runtime: RuntimeFileConfig = RuntimeFileConfig()
    initiative: InitiativeFileConfig = InitiativeFileConfig()
    voice: VoiceFileConfig = VoiceFileConfig()
    memory: MemoryFileConfig = MemoryFileConfig()
    jobs: JobsFileConfig = JobsFileConfig()


@dataclass(frozen=True)
class LaunchConfiguration:
    """The small set of runtime-significant values configurable at launch."""

    profile: str
    hardware: str
    camera: str
    cognition: str
    vision: str
    mode: str
    timezone: str
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
    memory_enabled: bool
    memory_database_path: Path | None
    jobs_enabled: bool
    jobs_database_path: Path | None
    jobs_auto_continue: bool
    jobs_heartbeat_seconds: float
    jobs_max_auto_steps: int
    jobs_scheduler_poll_seconds: float


HISTORICAL_DEFAULTS = LaunchConfiguration(
    profile="mira", hardware="virtual", camera="none", cognition="none", vision="none", mode="run",
    timezone="UTC",
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
    memory_enabled=False, memory_database_path=None,
    jobs_enabled=False, jobs_database_path=None, jobs_auto_continue=False,
    jobs_heartbeat_seconds=30.0, jobs_max_auto_steps=3,
    jobs_scheduler_poll_seconds=30.0,
)

_RUNTIME_KEYS = {
    "profile", "hardware", "camera", "cognition", "vision", "mode", "timezone",
}
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
_MEMORY_KEYS = {"enabled", "database_path"}
_JOBS_KEYS = {
    "enabled", "database_path", "auto_continue", "heartbeat_seconds",
    "max_auto_steps",
    "scheduler_poll_seconds",
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
    _reject_unknown(data, {"runtime", "initiative", "voice", "memory", "jobs"})
    runtime = _table(data, "runtime")
    initiative = _table(data, "initiative")
    voice = _table(data, "voice")
    memory = _table(data, "memory")
    jobs = _table(data, "jobs")
    _reject_unknown(runtime, _RUNTIME_KEYS, "runtime")
    _reject_unknown(initiative, _INITIATIVE_KEYS, "initiative")
    _reject_unknown(voice, _VOICE_KEYS, "voice")
    _reject_unknown(memory, _MEMORY_KEYS, "memory")
    _reject_unknown(jobs, _JOBS_KEYS, "jobs")

    if "enabled" in memory and not isinstance(memory["enabled"], bool):
        raise ConfigurationError("memory.enabled must be boolean")
    if "database_path" in memory and not isinstance(memory["database_path"], str):
        raise ConfigurationError("memory.database_path must be a string")
    memory_enabled = memory.get("enabled", False)
    configured_path = memory.get("database_path")
    if memory_enabled and (configured_path is None or not configured_path.strip()):
        raise ConfigurationError(
            "memory.database_path must be a non-empty string when memory is enabled"
        )
    database_path = None
    if configured_path is not None and configured_path.strip():
        database_path = Path(configured_path).expanduser()
        if not database_path.is_absolute():
            database_path = (path.parent / database_path).resolve()

    if "enabled" in jobs and not isinstance(jobs["enabled"], bool):
        raise ConfigurationError("jobs.enabled must be boolean")
    if "database_path" in jobs and not isinstance(jobs["database_path"], str):
        raise ConfigurationError("jobs.database_path must be a string")
    if "auto_continue" in jobs and not isinstance(jobs["auto_continue"], bool):
        raise ConfigurationError("jobs.auto_continue must be boolean")
    heartbeat_seconds = jobs.get("heartbeat_seconds", 30.0)
    if (isinstance(heartbeat_seconds, bool)
            or not isinstance(heartbeat_seconds, (int, float))
            or not math.isfinite(heartbeat_seconds) or heartbeat_seconds <= 0):
        raise ConfigurationError("jobs.heartbeat_seconds must be a positive number")
    max_auto_steps = jobs.get("max_auto_steps", 3)
    if (isinstance(max_auto_steps, bool) or not isinstance(max_auto_steps, int)
            or max_auto_steps <= 0):
        raise ConfigurationError("jobs.max_auto_steps must be a positive integer")
    scheduler_poll_seconds = jobs.get("scheduler_poll_seconds", 30.0)
    if (isinstance(scheduler_poll_seconds, bool)
            or not isinstance(scheduler_poll_seconds, (int, float))
            or not math.isfinite(scheduler_poll_seconds)
            or scheduler_poll_seconds <= 0):
        raise ConfigurationError("jobs.scheduler_poll_seconds must be a positive number")
    jobs_enabled = jobs.get("enabled", False)
    jobs_configured_path = jobs.get("database_path")
    if jobs_enabled and (jobs_configured_path is None or not jobs_configured_path.strip()):
        raise ConfigurationError(
            "jobs.database_path must be a non-empty string when jobs are enabled"
        )
    jobs_database_path = None
    if jobs_configured_path is not None and jobs_configured_path.strip():
        jobs_database_path = Path(jobs_configured_path).expanduser()
        if not jobs_database_path.is_absolute():
            jobs_database_path = (path.parent / jobs_database_path).resolve()

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
        if key == "timezone":
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError) as error:
                raise ConfigurationError(
                    f"unknown IANA timezone for runtime.timezone: {value!r}"
                ) from error
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
        RuntimeFileConfig(**runtime), InitiativeFileConfig(**initiative),
        VoiceFileConfig(**voice), MemoryFileConfig(memory_enabled, database_path),
        JobsFileConfig(
            jobs_enabled, jobs_database_path, jobs.get("auto_continue", False),
            float(heartbeat_seconds), max_auto_steps,
            float(scheduler_poll_seconds),
        ),
    )


def resolve_launch_configuration(
    cli_values: object, file_config: RuntimeFileConfiguration | None = None
) -> LaunchConfiguration:
    """Merge explicit CLI values over file values and historical defaults."""
    file_config = file_config or RuntimeFileConfiguration()
    runtime = file_config.runtime
    initiative = file_config.initiative
    voice = file_config.voice
    memory = file_config.memory
    jobs = file_config.jobs

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
        timezone=runtime.timezone or HISTORICAL_DEFAULTS.timezone,
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
        memory_enabled=memory.enabled,
        memory_database_path=memory.database_path if memory.enabled else None,
        jobs_enabled=jobs.enabled,
        jobs_database_path=jobs.database_path if jobs.enabled else None,
        jobs_auto_continue=jobs.auto_continue,
        jobs_heartbeat_seconds=jobs.heartbeat_seconds,
        jobs_max_auto_steps=jobs.max_auto_steps,
        jobs_scheduler_poll_seconds=jobs.scheduler_poll_seconds,
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

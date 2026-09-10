# Runtime startup configuration

Normal full-agentic operation uses one readable, startup-only TOML file:

```console
python main.py --config config/mira-agentic.toml
```

The checked-in file selects the full agentic camera, cognition, initiative,
console, and voice configuration, including the file-only local wake settings.
It supplies launch values before existing application setup rather than adding
implicit configuration discovery.

Mira's checked-in physical voice is ElevenLabs model `eleven_flash_v2_5`, voice
ID `pFZP5JQG7iQjIQuC4Bku`, at speed `1.1`. Normal startup requires the optional
ElevenLabs dependency and `ELEVENLABS_API_KEY` in the environment. The profile
also retains OpenAI model `gpt-4o-mini-tts` and voice `marin` as dormant
alternative-provider settings. An operator can explicitly select that
alternative without restating its model or voice:

```console
python main.py \
  --config config/mira-agentic.toml \
  --tts openai
```

This override is an explicit operator comparison or fallback choice, not an
automatic fallback when ElevenLabs credentials or service are unavailable.

## Schema

Only these three tables and keys are accepted:

```toml
[runtime]
profile = "mira"
hardware = "virtual"
camera = "picamera2"
cognition = "openai-responses"
vision = "openai-responses"
mode = "console"
timezone = "America/Toronto"

[initiative]
enabled = true
platform_attention = true
actions = true
messages = true
continuation = true
goal_closure = true

[voice]
enabled = true
wake_word_enabled = true
wake_words = ["mira", "mirror"]
tts = "espeak"
initial_timeout_seconds = 18
followup_timeout_seconds = 10
```

`hardware`, `camera`, and `cognition` accept the same values as their existing
CLI options. `runtime.mode` is exactly one of `run`, `console`, or `diagnostics`;
it maps to neither mode flag, `--console`, or `--diagnostics`, respectively.
The initiative values, `voice.enabled`, and `voice.wake_word_enabled` must be
TOML booleans. `voice.wake_words` must be a non-empty TOML array whose entries
are non-empty strings after trimming. Both voice timeouts must be positive TOML
numbers. All runtime values must be strings.
`voice.tts` is `"espeak"`, `"piper"`, `"openai"`, or `"elevenlabs"`; Piper additionally requires the
`voice.piper_model` path to a local `.onnx` model. `--tts` and `--piper-model`
provide narrow launch overrides. OpenAI TTS uses `voice.openai_tts_model` and
`voice.openai_tts_voice`, overridden by `--openai-tts-model` and
`--openai-tts-voice`; credentials remain environment-provided. ElevenLabs uses
`voice.elevenlabs_tts_model`, the required `voice.elevenlabs_tts_voice_id`, and
`voice.elevenlabs_tts_speed`, overridden by the corresponding
`--elevenlabs-tts-model`, `--elevenlabs-tts-voice-id`, and
`--elevenlabs-tts-speed` options. Speed defaults to `1.0`, accepts numeric values
from `0.7` through `1.2`, and values above `1.0` speed speech up. This is a
request-level override that does not modify the saved ElevenLabs voice; no
stability, similarity, style, or speaker-boost setting is overridden. Its API
key is environment-provided, not TOML.
Unknown tables, unknown keys, wrong types, unsupported values, and malformed
TOML fail before a profile or backend is constructed.

The file may be partial. Omitted values retain the historical defaults:

| Value | Default |
| --- | --- |
| `runtime.profile` | `"mira"` |
| `runtime.hardware` | `"virtual"` |
| `runtime.camera` | `"none"` |
| `runtime.cognition` | `"none"` |
| `runtime.vision` | `"none"` |
| `runtime.mode` | `"run"` |
| `runtime.timezone` | `"UTC"` |
| every `[initiative]` value | `false` |
| `voice.enabled` | `false` |
| `voice.wake_word_enabled` | `false` |
| `voice.wake_words` | `["mira"]` |
| `voice.tts` | `"espeak"` |
| `voice.piper_model` | unset |
| `voice.openai_tts_model` | `"gpt-4o-mini-tts"` |
| `voice.openai_tts_voice` | `"cedar"` |
| `voice.elevenlabs_tts_model` | `"eleven_flash_v2_5"` |
| `voice.elevenlabs_tts_voice_id` | unset (required when selected) |
| `voice.elevenlabs_tts_speed` | `1.0` (range `0.7`–`1.2`) |
| `voice.initial_timeout_seconds` | `18.0` |
| `voice.followup_timeout_seconds` | `10.0` |

There is no implicit configuration file. Without `--config`, all historical
CLI defaults and specialized operations remain unchanged.

`runtime.timezone` is an explicit IANA timezone name validated by the standard
library. The deterministic historical default is `UTC`; the checked-in Mira
agentic configuration explicitly uses `America/Toronto`. The runtime never
discovers or inherits the host operating system timezone, and there is no CLI
timezone override.

## Precedence and validation

Resolution applies historical defaults, then file values, then explicitly
supplied CLI values. Ordinary argparse defaults do not overwrite the file.
Scalar options such as `--camera picamera2` override their file value. Explicit
positive initiative flags can turn a configured `false` into `true`; omitting a
flag preserves the configured value. There is intentionally no matching
`--no-initiative-*` family. To turn a configured permission off, edit or select
another configuration file.

The positive `--voice` flag similarly overrides a missing or false
`voice.enabled`; omitting it preserves the configured value. Physical speech is
still only available with the Fusion HAT hardware backend. The timeout values
and wake settings have no CLI overrides and remain file-configured. Wake mode
is inert unless voice and the physical Fusion HAT provider are available. See
[Bounded voice conversation](voice-conversation.md) for the two-turn lifecycle
and intentionally excluded audio features.

Dependencies are checked once against the final merged values. For example,
initiative messages configured with `mode = "run"` become valid when the
operator explicitly adds `--console`.

`runtime.vision` accepts `"none"` or `"openai-responses"`; the matching explicit
CLI override is `--vision`. A final effective vision value other than `none`
requires both an effective camera and cognition backend. The checked-in
`config/mira-agentic.toml` enables `openai-responses` vision.

Configuration is loaded once at startup. There is no discovery, inheritance,
layering, named preset, environment interpolation, or hot reload. Relative
paths are relative to the current working directory. `startup_prompt`,
`--camera-test`, `--fusion-servo-test`, `--fusion-battery-test`, and `--no-color`
remain CLI-only.

OpenAI credentials remain environment-provided and are not stored in runtime
TOML configuration. In particular, `OPENAI_API_KEY` is not part of this schema;
an API-key table or key is rejected as unknown.

For a focused experiment, combine a file with an existing override:

```console
python main.py --config config/mira-agentic.toml --no-color
```

The explicit equivalent remains available for testing and diagnostics:

```console
python main.py \
  --camera picamera2 \
  --cognition openai-responses \
  --vision openai-responses \
  --initiative \
  --initiative-platform-attention \
  --initiative-actions \
  --initiative-messages \
  --initiative-continuation \
  --initiative-goal-closure \
  --voice \
  --console
```

# Runtime startup configuration

Normal full-agentic operation uses one readable, startup-only TOML file:

```console
python main.py --config config/mira-agentic.toml
```

The checked-in file selects the full agentic camera, cognition, initiative,
console, and voice configuration, including the file-only local wake settings.
It supplies launch values before existing application setup rather than adding
implicit configuration discovery.

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
`voice.tts` is `"espeak"` or `"piper"`; Piper additionally requires the
`voice.piper_model` path to a local `.onnx` model. `--tts` and `--piper-model`
provide narrow launch overrides for these two values.
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
| every `[initiative]` value | `false` |
| `voice.enabled` | `false` |
| `voice.wake_word_enabled` | `false` |
| `voice.wake_words` | `["mira"]` |
| `voice.tts` | `"espeak"` |
| `voice.piper_model` | unset |
| `voice.initial_timeout_seconds` | `18.0` |
| `voice.followup_timeout_seconds` | `10.0` |

There is no implicit configuration file. Without `--config`, all historical
CLI defaults and specialized operations remain unchanged.

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

# Local runtime console

`attention` reports whether initiative is enabled, its idle/in-flight/completed/
failed state, latest trigger and source, latest action/status, and the latest
bounded autonomous response when available. That response is volatile diagnostic state shown only
by this command; aggregate `status` and logs exclude its content, and later
cognition never receives it.

Initiative uses independent permissions. No flags means operator-only
cognition; `--initiative` permits bounded read-only `inspect_self` or
`observe_scene` acquisition where available and the single bounded
`schedule_followup` semantic effect; `--initiative-actions` permits
`orient_body` on a nonphysical body; and `--initiative-messages` permits
`address_operator`. The latter currently requires `--console`, because the local
console is the only configured operator transport. No flag silently enables
another. With both effect permissions, both tools are offered but the runtime accepts
only one request total unless bounded continuation is explicitly enabled. Goal
closure requires initiative only and still runs only after an actual semantic
effect, never after read-only acquisition alone.

The console concurrently waits for selector-based input and a transient outbound
message queue. It renders an accepted message immediately as `Mira: ...`, cancels
the losing wait, and redraws the prompt. This uses no stdin executor, polling, or
sleep loop. EOF, quit, cancellation, and shutdown clean pending waits. Questions
are delivered once and create no pending reply. Messages are not a console
history, RuntimeState, WorkingMemory, or EventBus history.

Run `python main.py --console` for the asynchronous local console. Normal
full-agentic Mira startup uses
`python main.py --config config/mira-agentic.toml`; the file selects console mode
and the established initiative permissions. See [Runtime startup
configuration](configuration.md) for its strict schema and CLI override rules. Platform
sampling continues while it waits, but routine platform heartbeat logs are
suppressed in this interactive mode. Authoritative platform state still updates,
and thermal and memory-pressure warnings and clear transitions remain active.
Non-console/headless operation retains the normal heartbeat logs. Reports read
application-owned authoritative state and never probe a platform, hardware
backend, or body pose.

The interactive console uses restrained semantic ANSI colours solely to improve
human scanability. Mira's operator messages are presented in bright magenta/pink,
the prompt is subdued cyan, and known diagnostic states and first-party log
categories receive consistent semantic colours. The underlying messages, state,
and log words remain plain semantic text. First-party structured log timestamps
are visually subdued in ANSI mode, without changing their text, format, or
spacing. Colour is enabled only when the output stream is a TTY; redirected
output and logs are therefore plain. Set `NO_COLOR` (to any value), or pass
`--no-color`, to disable colour explicitly.

The supported commands are exactly:

- `status`, `platform`, and `hardware` for existing reports;
- `body` and `body orient <yaw> <pitch>` for body state and the semantic body API;
- `camera status` and `camera capture <output_path>` for the configured camera;
- `presence` for current semantic presence;
- `simulate presence <on|off>` for explicitly synthetic development input;
- `ask <message>` for one independent text cognition request;
- `memory` for working-memory count/capacity metadata (not retained text);
- `memory clear` to synchronously forget all retained session turns without
  changing runtime state or configured backends;
- `goal` to show the current intentional state;
- `goal clear` to apply an explicit local operator override without changing
  RuntimeState or WorkingMemory;
- `attention` to show the latest volatile initiative and outcome diagnostics;
- `help` (or `?`), `quit`, and `exit`.

Only the `simulate` namespace denotes synthetic input. It translates to the
ordinary semantic presence-ingestion API with source `virtual_scenario`.
Command vocabulary is case-insensitive, while `shlex` parsing preserves payload
tokens and reports malformed quoting without a traceback. EOF also ends cleanly.
The text following `ask` is instead treated as raw natural-language payload, so
apostrophes and punctuation do not require shell quoting. Cognition errors are
reported without ending the console session.

With `--initiative-continuation`, `attention` additionally reports the latest
continuation state, action, action status, and bounded response. The flag
requires initiative plus both action and message permissions; the latter still
requires `--console`. It permits one independent second request only after an
applied first effect, and that request may use at most one different capability.
Thus one attention episode can request at most two distinct semantic effects.
Rejected first effects are not retried or followed by a continuation. The
`CONTINUATION` log category uses the existing high-attention colour palette.

## Platform attention option

`--initiative-platform-attention` requires `--initiative` and adds existing
platform thermal-warning and memory-pressure raised/cleared transitions as
attention sources. Without it, initiative retains its reflex-body-only default.
The flag grants no additional effects: temporal scheduling comes from the base
initiative policy, while action, message, continuation, and goal-closure
permissions remain independent. The `attention` command naturally reports the
latest observation kind and source through its existing trigger/source fields;
it is not an event viewer. Contending observations are deliberately suppressed,
not queued.

## Inspection diagnostics

`attention` includes volatile last-inspection state, area, and status. There is no
inspection console command; use ordinary `ask` cognition. Inspection facts are
not retained in diagnostics. See [Bounded semantic self-inspection](self-inspection.md).

## Temporal follow-up control

`followup` shows either `state: none` or the one pending follow-up's bounded
relative delay, remaining seconds, and purpose. `followup clear` cancels it.
There is intentionally no add/list/scheduler command. `[TEMPORAL]` logs use a
restrained colour and omit purpose text. See
[Bounded temporal follow-up](temporal-followup.md).

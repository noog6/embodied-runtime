# Local runtime console

`attention` reports whether initiative is enabled, its idle/in-flight/completed/
failed state, latest trigger and source, latest action/status, and the latest
bounded autonomous response when available. That response is volatile diagnostic state shown only
by this command; aggregate `status` and logs exclude its content, and later
cognition never receives it.

Initiative uses independent permissions. No flags means operator-only
cognition; `--initiative` is exactly read-only; `--initiative-actions` permits
`orient_body` on a nonphysical body; and `--initiative-messages` permits
`address_operator`. The latter currently requires `--console`, because the local
console is the only configured operator transport. No flag silently enables
another. With both effect permissions, both tools are offered but the runtime
accepts only one request total. Goal closure requires initiative plus at least
one effect permission.

The console concurrently waits for selector-based input and a transient outbound
message queue. It renders an accepted message immediately as `Mira: ...`, cancels
the losing wait, and redraws the prompt. This uses no stdin executor, polling, or
sleep loop. EOF, quit, cancellation, and shutdown clean pending waits. Questions
are delivered once and create no pending reply. Messages are not a console
history, RuntimeState, WorkingMemory, or EventBus history.

Run `python main.py --console` for the asynchronous local console. Platform
sampling and heartbeats continue while it waits. Reports read application-owned
authoritative state and never probe a platform, hardware backend, or body pose.

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

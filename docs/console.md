# Local runtime console

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
- `help` (or `?`), `quit`, and `exit`.

Only the `simulate` namespace denotes synthetic input. It translates to the
ordinary semantic presence-ingestion API with source `virtual_scenario`.
Command vocabulary is case-insensitive, while `shlex` parsing preserves payload
tokens and reports malformed quoting without a traceback. EOF also ends cleanly.
The text following `ask` is instead treated as raw natural-language payload, so
apostrophes and punctuation do not require shell quoting. Cognition errors are
reported without ending the console session.

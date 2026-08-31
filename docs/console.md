# Local runtime console

Run `python main.py --console` to start the local, line-oriented inspection
console. Its prompt comes from the loaded robot profile (for example, `mira>`),
and its reports are direct interactive terminal output. Runtime log records
remain separate and retain their timestamped format.

The console is a read-only projection of application-owned authoritative
runtime state. It owns no lifecycle, platform, hardware, or robot state and
does not independently probe the host or robot hardware. The application-owned
platform monitor continues sampling and producing heartbeats while the console
waits asynchronously for input, so each report reflects the latest installed
platform snapshot.

The supported commands are exactly:

- `status`: show a compact runtime, platform, and hardware overview;
- `platform`: show the latest host-platform snapshot;
- `hardware`: show the selected robot hardware backend;
- `help` (or `?`): list supported commands;
- `quit` and `exit`: leave the console and request orderly runtime shutdown.

EOF also ends the session cleanly. This console has no network access, event
history, simulation, shell access, or runtime/hardware control commands.

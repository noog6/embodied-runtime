# Local runtime console

`job files JOB<n> [directory]` lists at most one bounded Workspace directory
page without recursion. `job file JOB<n> <logical-path> [offset_chars]` reads at
most 8,000 Unicode characters and reports its SHA-256 content version, byte
size, returned range, and continuation offset. Both commands are read-only,
require an existing Job, use logical paths rather than host paths, and never
create a Workspace. The console has no Workspace write or delete command.

`attention` reports whether initiative is enabled, its idle/in-flight/completed/
failed state, latest trigger and source, latest action/status, and the latest
bounded autonomous response when available. That response is volatile diagnostic state shown only
by this command; aggregate `status` and logs exclude its content, and later
cognition never receives it.

Initiative uses independent permissions. No flags means operator-only
cognition; `--initiative` permits bounded read-only `inspect_self`,
`observe_scene`, `recall_memory`, or `inspect_run_history` acquisition where
available and the single bounded
`schedule_followup` semantic effect; `--initiative-actions` permits
`orient_body` on a nonphysical body; and `--initiative-messages` permits
`address_operator`. The latter currently requires `--console`, because the local
console is the only configured operator transport. No flag silently enables
another. With both effect permissions, both tools are offered but the runtime accepts
only one request total unless bounded continuation is explicitly enabled. Goal
closure requires initiative only and still runs only after an actual semantic
effect, never after read-only acquisition alone.

The console concurrently waits for selector-based input and a transient outbound
message queue. The queue accepts runtime-originated notifications and
operator-directed deliveries requested through the current console or another
dialogue channel. Both are non-dialogue deliveries and create no pending console
reply, cognition episode, or extra WorkingMemory turn. There is no new `send` or
`deliver` command: delivery is requested through normal `ask ...` or voice
cognition. It renders an accepted message immediately as `Mira: ...`, keeps
the input wait alive, and redraws the prompt, draft, and cursor. This uses no stdin executor, polling, or
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
- `voice` to start one bounded voice session;
- `memory` for working-memory count/capacity metadata (not retained text);
- `job update JOB<n> --description <text>` to change only a durable Job's
  responsibility description for future occurrences; it preserves the Job's
  identity, history, schedule, and Workspace, and is rejected while that same
  Job is currently executing;
- `job schedule JOB<n>` to inspect the single durable schedule;
  `job schedule JOB<n> daily HH:MM [--timezone ZONE]` or
  `job schedule JOB<n> hourly [--minute M] [--timezone ZONE]` to replace it;
  `job unschedule JOB<n>` to remove it. Hourly minute defaults to 0 and must be
  an integer from 0 through 59; omitted timezone uses the application timezone.
  See [wall-clock scheduling](jobs.md#daily-and-hourly-wall-clock-activation) for
  DST, contention, restart, and durable deduplication semantics;
- `job trigger JOB<n> runtime_ready` to durably activate an enabled Job once
  when each future runtime instance becomes ready; `job trigger JOB<n>` reports
  configured triggers and `job untrigger JOB<n> runtime_ready` removes it;
- `memory clear` to synchronously forget all retained session turns without
  changing runtime state or configured backends;
- `memory persistent`, `memory entity add`, `memory entity find`, `memory alias
  add`, `memory find`, `memory add`, `memory show`, and `memory list` for the
  persistent-memory administration described below;
- `goal` to show the current intentional state;
- `goal clear` to apply an explicit local operator override without changing
  RuntimeState or WorkingMemory;
- `attention` to show the latest volatile initiative and outcome diagnostics;
- `followup` to show the pending temporal follow-up and `followup clear` to
  cancel it;
- `runs` to list at most the newest 20 recorded runs, `run show R<n>` to inspect
  validated metadata, and `run grep R<n> <text>` for at most 50
  case-insensitive literal (not regex) log matches with line numbers;
- `help` (or `?`), `quit`, and `exit`.

Only the `simulate` namespace denotes synthetic input. It translates to the
ordinary semantic presence-ingestion API with source `virtual_scenario`.
Command vocabulary is case-insensitive, while `shlex` parsing preserves payload
tokens and reports malformed quoting without a traceback. EOF also ends cleanly.
The text following `ask` is instead treated as raw natural-language payload, so
apostrophes and punctuation do not require shell quoting. Cognition errors are
reported without ending the console session.

Run browsing reads only exact identities beneath the CLI-selected history root;
it accepts no arbitrary paths and does not follow run or artifact symlinks.
Malformed history is reported without a traceback. A persisted `started` status
is displayed as historical fact, never inferred to mean running or crashed.
These administrative commands do not enter cognition, WorkingMemory, runtime
state, persistent memory, or self-inspection. See [Run history](run-history.md).

During shutdown, `[APP] stopped` means that application-owned resources have
finished stopping. The subsequent `[PROCESS] asyncio_cleanup` duration measures
runner cleanup (including pending-task, async-generator, and default-executor
shutdown), and `[PROCESS] main status=returning` is the final boundary before the
CLI returns toward interpreter exit. A bounded thread line reports any live
non-daemon threads that could still hold the process open.

## Input editing and interrupts

On an interactive terminal, Up/Down browse the last 500 nonblank commands from
this console session without wrapping at the oldest entry. Down past the newest
entry restores the unfinished draft. Recalled commands are editable copies;
Enter submits the edited text and adds a new history entry. Spacing, quotes,
Unicode, and repeated submissions are preserved. This history is private,
memory-only, discarded on session exit, and has no model-facing API or new
logging. It is separate from durable run browsing and conversation history.

Left/Right, Home/End, Backspace, and Delete edit one logical input line. Long
commands may wrap over screen rows; they remain one command, including while
resizing the SSH terminal or receiving operator messages and runtime logs.
Bracketed paste replaces pasted line breaks with spaces; there is no multiline
syntax or multiline editor. `--no-color` and `NO_COLOR` disable styling while
retaining the terminal controls needed to edit. When stdin or stdout is not a
TTY, the original asynchronous plain line reader is used; redirected stdout
receives no editor escape sequences.

The default `--ctrl-c-shutdown auto` clears unfinished console input and resets
history navigation. It never submits the draft or interrupts executing commands,
cognition, Jobs, or the runtime. `--ctrl-c-shutdown enabled` requests graceful
shutdown instead. `disabled` also keeps headless mode running on Ctrl+C. These
values can be set as `ctrl_c_shutdown = "auto"` under `[runtime]` in TOML;
explicit CLI overrides TOML, then defaults. `auto` enables shutdown without a
console. `exit`, `quit`, EOF (Ctrl+D at an empty prompt), and SIGTERM retain
graceful shutdown. Signal handlers remain installed through application cleanup,
so repeated interrupts cannot escalate into asyncio's default interrupt handling.
Signals arriving after quit, EOF, or diagnostics have begun application cleanup
wait for that cleanup to finish. They only cancel console/diagnostics execution
while startup or commands are still active. Task cancellation remains independent
and propagates; cleanup errors are not converted into successful signal exits.

The interactive implementation uses `prompt_toolkit` 3's async API rather than
custom raw-mode escape parsing, Unicode width calculations, and wrapped-row
redrawing. It uses selector input, not blocking `input()` in an executor. Terminal
mode, input readers, output routing, and previous signal handlers are restored
on exit or failure. Matching terminal-bound Python stdout/stderr and the runtime's
duplicated stderr logging handler use redraw-aware output. Redirected log/file
streams retain their destinations. Native code or subprocesses writing directly
to the terminal file descriptor bypass this Python routing and can still disturb
the display; redraw with Ctrl+L if needed.

The supported target is Python 3.13 on Linux/Raspberry Pi OS over a UTF-8,
VT100-compatible SSH terminal with an accurate `TERM`. Display width for unusual
emoji or combining sequences depends on terminal/font agreement with Unicode
width tables. Terminals without cursor-position reports may use
`PROMPT_TOOLKIT_NO_CPR=1` to suppress the library's capability warning. Windows
console signal behavior is not covered by these Linux PTY tests.

### Manual acceptance on the Pi over SSH

Use an isolated foreground launch with virtual hardware; leave the live service
alone. Install the project dependencies in the development virtual environment,
then run `python main.py --hardware virtual --camera none --console --no-color`.

1. Submit a long harmless command such as
   `job add "SSH history check" --description "a deliberately long description with spaces and quotes to wrap across several rows"`
   with Jobs disabled; the rejection is expected and does not create a Job.
   Type a draft, press Up, edit within the recalled command with Left/Right,
   Home/End, Backspace/Delete, and Enter. Recall again to verify the original
   entry, and use Down to restore a draft. Include `café 界` to check UTF-8.
2. Resize the SSH window while editing a recalled command. Keep the cursor in
   its middle and check that typing inserts at that position. While a runtime
   log or operator message arrives in an authorized development session, check
   that text and cursor survive. Automated PTY tests inject both kinds of output
   without requiring cognition, credentials, or hardware.
3. Press Ctrl+C repeatedly on a draft, then submit `status`. Check that no draft
   was submitted and the runtime still responds. In a development session with
   an authorized cognition request running, repeat Ctrl+C and verify it finishes.
4. Use `quit`, relaunch, and verify history is empty. Repeat with `exit` and with
   Ctrl+D at an empty prompt; verify normal shell echo and cursor behavior.
5. Relaunch with `--ctrl-c-shutdown enabled`; Ctrl+C should stop cleanly. Launch
   with `--mode run` and no console; default Ctrl+C should also stop cleanly.
   With `--mode run --ctrl-c-shutdown disabled`, Ctrl+C should leave it running;
   send SIGTERM to that isolated PID to stop it.
6. Run `printf 'status\nquit\n' | python main.py --hardware virtual --camera none --console --no-color > /tmp/console-check.txt`
   and verify ordinary text with no editor escape sequences.

Automated focused checks (the `test` extra adds the `pyte` screen emulator):
`python -m pip install -e '.[test]'`, then
`python -m unittest tests.test_terminal_editor tests.test_console_signals tests.test_console tests.test_configuration tests.test_runtime tests.test_interaction tests.test_logging_config tests.test_console_style tests.test_run_history`.

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

`followup` shows either `state: none` or the one pending or due-pending
follow-up's bounded relative delay, remaining seconds, and purpose. `followup
clear` cancels it.
There is intentionally no add/list/scheduler command. `[TEMPORAL]` logs use a
restrained colour and omit purpose text. See
[Bounded temporal follow-up](temporal-followup.md).

A temporal `followup` state of `due_pending` means its timer is due and its one
exact-goal attention opportunity is waiting for the current single-flight episode
to finish. Self-inspection remains `temporal_followup_pending=true` in both
`pending` and `due_pending`; goal change, clear, completion, or shutdown removes it.

## Attention episodes

`ask <message>` is the only deliberative console command and labels its operator
episode with source `console`. Administrative commands remain direct runtime
operations. `attention` separates shared current/last episode lifecycle fields
(including trigger and source) from the last autonomous-initiative diagnostics;
it never displays the operator utterance as an episode concern.

# Memory commands

`memory` and `memory clear` continue to inspect and clear bounded, volatile
working memory. Durable operator administration uses `memory persistent`,
`memory entity add`, `memory entity find`, `memory alias add`, `memory find`,
`memory add`, `memory show`, and `memory list`. Durable lookup is exact after
case/Unicode/whitespace normalization; it is not fuzzy or semantic. Full syntax
is documented in [`persistent-memory.md`](persistent-memory.md).

Workspace listings distinguish images and show media type, dimensions, encoded
size and exact content version. `job file` remains text-only. Image capture, save,
interpretation and MMS delivery use ordinary `ask` episodes; see
[Workspace image operator commands](workspace-images.md#post-merge-validation-for-nick).

To authorize one operator image save, enter a complete standalone command such as
`ask save image {"job":"JOB7","path":"photos/frame.jpg","mode":"create"}`
with an existing exact Job and intended path/mode. Natural-language save requests
do not grant durable-write authority. The runtime enforces the scope before
storage; capture and save remain separate budgeted operations.

## Carry-forward focus administration

`job focus enable|disable|show JOB<n>`, `job focus snapshot RUN<n>`,
`job focus seed JOB<n> RUN<n> --question "Question?" [--status open|blocked]
[--blocker "Explanation"]`, and `job focus clear JOB<n> --reason "Explanation"`
use exact catalog IDs. Mutation rejects any live context for the same Job. Seed
requires an exact completed source Run belonging to that Job and records operator
authorship; its source reference does not establish that its text appeared in the
report. See [Carry-forward Job focus](job-focus.md) for bounds, transaction and
provenance rules, and the manual JOB15/RUN391 pilot.

Procedure administration and the exact `job procedure` command grammar, including
explicit experimental Job setup and retained Run inspection, are documented in
[Job procedures](job-procedures.md).

# Bounded semantic self-inspection

`inspect_self(area)` is the runtime's single read-only inspection capability.
Its exact areas are `network`, `storage`, `camera`, `runtime`, and `earcons`; aliases,
multiple areas, extra arguments, commands, and model-selected paths are rejected.
Immutable `SelfInspectionResult` and `SelfInspectionFact` values carry a small
provider-neutral summary. `RobotApplication` validates and executes the semantic
request, while the injectable `HostSelfInspector` owns passive host reads.

Operator cognition may make one tool call and use its provider-local result
continuation. An autonomous episode may spend either or both of its two acquisition
attempts on inspection, including the same capability for different exact areas.
Every attempt, including a rejection, consumes a slot. An inspection causes one
independent bounded follow-up decision with fresh `RuntimeState`, the exact same
`ActiveGoal`, the episode-start `WorkingMemory` snapshot, original semantic
observation, and ordered request-local evidence. After the first attempt it exposes
the remaining acquisition tools plus effects; after the second it exposes effects
only. If it applies effect one, the existing continuation may apply one distinct
second effect. Thus inspection does not count as an effect: the ceilings remain two
acquisition attempts and two semantic effects. Outcome evaluation receives all
bounded evidence separately and never runs for inspection alone.

Storage always uses `shutil.disk_usage("/")`. Network inspection sorts and caps
local interfaces at eight and reads only local kernel interface/default-route
metadata; it performs no connectivity test and cannot claim Internet reachability.
Camera inspection reports application-owned resource readiness without capture.
Runtime inspection exposes only bounded capability/lifecycle metadata, never goal
prose, memory text, messages, environment, credentials, or logs. Optional missing
host facts become `unavailable`; failure rejects the inspection without automatic
retry, is grounded honestly, and still consumes its acquisition slot.

There is no shell, subprocess capability, arbitrary filesystem access, outbound
probe, image capture, polling, history, new event, CLI flag, or TOML key. Power and
throttle inspection is deferred until a clean runtime/platform abstraction exists;
no Raspberry Pi command runner is introduced.

Phase 15 adds the single runtime-area fact
`temporal_followup_pending=true|false`. It reveals no purpose, due point, task,
or scheduler internals. Scheduling itself is a semantic effect, not inspection.

The `earcons` area projects the shared semantic cue catalog plus volatile
`last_attempt` and `last_played` evidence from `EarconPlayer`. An attempt may be
`played`, `skipped`, or `failed`; only `last_played` identifies the most recent cue
whose output completed successfully. Even that is runtime playback evidence, not
proof that a human heard it. Output availability is reported without guessing why
an output adapter is absent. Inspection neither acquires the speaker nor emits a
sound, and activity resets with a new player/runtime instance.

Operator and autonomous attention episodes may use self-inspection as one of at
most two ordered read-only acquisition attempts. Successful facts are
runtime-produced and authoritative for the inspected area; rejected attempts are
represented honestly and consume their slot.

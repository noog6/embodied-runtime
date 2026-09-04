# Bounded semantic self-inspection

`inspect_self(area)` is the runtime's single read-only inspection capability.
Its exact areas are `network`, `storage`, `camera`, and `runtime`; aliases,
multiple areas, extra arguments, commands, and model-selected paths are rejected.
Immutable `SelfInspectionResult` and `SelfInspectionFact` values carry a small
provider-neutral summary. `RobotApplication` validates and executes the semantic
request, while the injectable `HostSelfInspector` owns passive host reads.

Operator cognition may make one tool call and use its provider-local result
continuation. Autonomous Request A may instead spend its separate budget of one
inspection per attention episode. An applied inspection can cause one new,
independent follow-up decision with fresh `RuntimeState`, the exact same
`ActiveGoal`, the episode-start `WorkingMemory` snapshot, original semantic
observation, and inspection result. That request exposes effects only, never
inspection or goal tools. If it applies effect one, the existing single Phase 10
continuation may apply one distinct second effect. Thus inspection does not count
as an effect: the ceilings remain one inspection and two semantic effects. Outcome
evaluation receives inspection separately and never runs for inspection alone.

Storage always uses `shutil.disk_usage("/")`. Network inspection sorts and caps
local interfaces at eight and reads only local kernel interface/default-route
metadata; it performs no connectivity test and cannot claim Internet reachability.
Camera inspection reports application-owned resource readiness without capture.
Runtime inspection exposes only bounded capability/lifecycle metadata, never goal
prose, memory text, messages, environment, credentials, or logs. Optional missing
host facts become `unavailable`; failure rejects the inspection and ends that path
without retry or an effect follow-up.

There is no shell, subprocess capability, arbitrary filesystem access, outbound
probe, image capture, polling, history, new event, CLI flag, or TOML key. Power and
throttle inspection is deferred until a clean runtime/platform abstraction exists;
no Raspberry Pi command runner is introduced.

Phase 15 adds the single runtime-area fact
`temporal_followup_pending=true|false`. It reveals no purpose, due point, task,
or scheduler internals. Scheduling itself is a semantic effect, not inspection.

"""Small local development and control interface for the running application."""

import asyncio
from collections.abc import Callable
import math
import os
from pathlib import Path
import re
import shlex
import sys
import time
from typing import TextIO

from embodied_runtime.app import RobotApplication
from embodied_runtime.cognition import CognitionError
from embodied_runtime.platform import PlatformSnapshot
from embodied_runtime.interaction import (
    CONSOLE_ADMINISTRATIVE, CONSOLE_DIALOGUE, ConsoleOperatorMessageChannel,
    InteractionContext,
)
from embodied_runtime.memory import NewMemoryLink, NewMemoryPayload, StoredMemory
from embodied_runtime.console_style import ConsoleStyle, colour_enabled
from embodied_runtime.run_history import (
    DEFAULT_HISTORY_ROOT, MAX_GREP_QUERY_LENGTH, RunDataUnavailable,
    RunMetadataError, UnsupportedRunSchema, canonical_run_id, discover_run_ids,
    grep_run_log, read_run_record,
)


class ConsoleTerminalError(RuntimeError):
    """Raised when cancellable terminal input is unavailable."""


def _catalog_id(value: str, prefix: str) -> int | None:
    match = re.fullmatch(fr"(?i:{prefix})([1-9]\d*)", value)
    return int(match.group(1)) if match else None


class RuntimeConsole:
    """Interpret a deliberately small set of local development commands."""

    def __init__(
        self,
        application: RobotApplication,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        history_root: Path = DEFAULT_HISTORY_ROOT,
    ) -> None:
        self._application = application
        self._monotonic = monotonic
        self._history_root = history_root

    @property
    def prompt(self) -> str:
        return f"{self._application.profile.identifier}> "

    @property
    def heading(self) -> str:
        return f"{self._application.profile.name} Runtime Console"

    @property
    def operator_message_prefix(self) -> str:
        return self._application.profile.name

    @property
    def administrative_interaction(self) -> InteractionContext:
        """Classify local commands that do not enter cognition."""
        return CONSOLE_ADMINISTRATIVE

    def execute(self, command: str) -> tuple[str, bool]:
        """Return report text and whether the session should terminate."""
        raw_parts = command.lstrip().split(maxsplit=1)
        if raw_parts and raw_parts[0].lower() in {"ask", "voice"}:
            return "This command requires an active asynchronous console session.", False
        try:
            words = shlex.split(command)
        except ValueError as error:
            return f"Unable to parse command: {error}.", False
        if not words:
            return "", False
        vocabulary = [word.lower() for word in words]
        if vocabulary in (["quit"], ["exit"]):
            return "", True
        if vocabulary in (["help"], ["?"]):
            return self._help(), False
        if vocabulary == ["status"]:
            return self._status(), False
        if vocabulary == ["platform"]:
            return self._platform(), False
        if vocabulary == ["hardware"]:
            return self._hardware(), False
        if vocabulary == ["body"]:
            return self._body(), False
        if vocabulary == ["camera", "status"]:
            return self._camera(), False
        if vocabulary == ["presence"]:
            return self._presence(), False
        if vocabulary == ["memory"]:
            return self._memory(), False
        if vocabulary == ["jobs"]:
            return self._jobs(), False
        if vocabulary[:2] == ["job", "show"]:
            return self._job_show(words), False
        if vocabulary[:2] == ["job", "runs"]:
            return self._job_runs(words), False
        if vocabulary and vocabulary[0] == "memory" and vocabulary != ["memory", "clear"]:
            return self._persistent_memory_command(words), False
        if vocabulary == ["goal"]:
            return self._goal(), False
        if vocabulary == ["attention"]:
            return self._attention(), False
        if vocabulary == ["followup"]:
            return self._followup(), False
        if vocabulary and vocabulary[0] == "runs":
            return (self._runs() if len(words) == 1 else "Usage: runs."), False
        if vocabulary and vocabulary[0] == "run":
            return self._run_command(words), False
        if vocabulary == ["followup", "clear"]:
            cleared = self._application.clear_temporal_followup()
            return "Temporal follow-up\n  cleared:       " + str(cleared).lower(), False
        if vocabulary == ["goal", "clear"]:
            cleared = self._application.clear_goal()
            return "Active goal\n  cleared:       " + str(cleared).lower(), False
        if vocabulary == ["memory", "clear"]:
            cleared = self._application.working_memory.clear()
            return "\n".join((
                "Working memory",
                f"  cleared:       {cleared}",
                "  turns:         0",
            )), False
        if (
            vocabulary[:2] == ["body", "orient"]
            or vocabulary[:2] == ["simulate", "presence"]
            or vocabulary[:2] == ["camera", "capture"]
        ):
            return "This command requires an active asynchronous console session.", False
        return f"Unknown command: {words[0]}. Type 'help' for commands.", False

    async def execute_async(self, command: str) -> tuple[str, bool]:
        """Execute commands, including semantic operations that must be awaited."""
        raw_parts = command.lstrip().split(maxsplit=1)
        if raw_parts and raw_parts[0].lower() == "voice":
            if len(raw_parts) != 1:
                return "Usage: voice.", False
            return await self._application.voice.start(source="console"), False
        if raw_parts and raw_parts[0].lower() == "ask":
            message = raw_parts[1] if len(raw_parts) == 2 else ""
            if not message.strip():
                return "Usage: ask <message>.", False
            try:
                response = await self._application.handle_operator_utterance(
                    message, interaction=CONSOLE_DIALOGUE
                )
            except (CognitionError, RuntimeError, ValueError) as error:
                return f"Cognition request failed: {error}.", False
            return f"{self._application.profile.name}: {response}", False
        try:
            words = shlex.split(command)
        except ValueError as error:
            return f"Unable to parse command: {error}.", False
        lowered = [word.lower() for word in words]
        if lowered[:2] == ["camera", "capture"]:
            if len(words) != 3:
                return "Usage: camera capture <output_path>.", False
            try:
                frame = self._application.capture_camera_frame()
                Path(words[2]).write_bytes(frame.data)
            except (RuntimeError, OSError) as error:
                return f"Camera capture failed: {error}.", False
            summary = self._application.camera_summary()
            backend = "unknown" if summary is None else summary.backend
            return "\n".join(
                (
                    "Camera capture",
                    f"  backend:       {backend}",
                    f"  width:         {frame.width}",
                    f"  height:        {frame.height}",
                    f"  media_type:    {frame.media_type}",
                    f"  bytes:         {len(frame.data)}",
                    f"  output:        {words[2]}",
                    "  status:        ok",
                )
            ), False
        if lowered[:2] == ["body", "orient"]:
            if len(words) != 4:
                return "Usage: body orient <yaw> <pitch>.", False
            try:
                yaw, pitch = float(words[2]), float(words[3])
                await self._application.set_body_orientation(
                    yaw_degrees=yaw, pitch_degrees=pitch, source="console"
                )
            except (ValueError, RuntimeError) as error:
                return f"Invalid body orientation: {error}.", False
            return self._body(), False
        if lowered[:2] == ["simulate", "presence"]:
            if len(words) != 3 or lowered[2] not in {"on", "off"}:
                return "Usage: simulate presence <on|off>.", False
            present = lowered[2] == "on"
            previous = self._application.runtime_state.presence
            await self._application.observe_presence(
                present=present, source="virtual_scenario"
            )
            if previous is None or previous.present != present:
                import logging
                logging.getLogger(__name__).info(
                    "[SCENARIO] presence=%s source=virtual_scenario",
                    "present" if present else "absent",
                )
            return self._presence(), False
        return self.execute(command)

    @staticmethod
    def _help() -> str:
        return "\n".join(
            (
                "Commands",
                "  status                         Show current runtime overview",
                "  platform                       Show current host platform state",
                "  hardware                       Show robot hardware backend",
                "  body                           Show current body state",
                "  body orient <yaw> <pitch>      Set semantic body orientation",
                "  camera status                  Show configured camera resource",
                "  camera capture <output_path>   Capture one JPEG to an explicit path",
                "  presence                       Show current presence state",
                "  simulate presence <on|off>     Inject virtual presence",
                "  ask <message>                  Send one text cognition request",
                "  voice                          Start one bounded voice session",
                "  memory                         Show working-memory metadata",
                "  jobs                           List the entire durable Job catalog",
                "  job show JOB<n>                Show one Job and its assignment",
                "  job runs JOB<n>                List durable occurrences of one Job",
                "  memory clear                   Clear session working memory",
                "  memory persistent              Show persistent-memory state",
                "  memory entity add <entity_type> <canonical_name> Create a durable entity",
                "  memory entity find <name>       Exact entity lookup alias",
                "  memory alias add <ENTn> <alias> Add a durable entity alias",
                "  memory find <name>              Exact entity and memory lookup",
                "  memory add <kind> <summary> [options] Create a durable text memory",
                "  memory show <MEMn>              Show one durable memory",
                "  memory list <ENTn>              List an entity's memories",
                "  goal                           Show current active goal",
                "  goal clear                     Clear current active goal",
                "  attention                      Show initiative attention state",
                "  followup                       Show pending temporal follow-up",
                "  followup clear                 Cancel pending temporal follow-up",
                "  runs                           List recent recorded runs",
                "  run show R<n>                  Show one recorded run",
                "  run grep R<n> <text>           Search one run's runtime log",
                "  help                           Show this help",
                "  quit                           Stop the console and runtime",
                "  exit                           Stop the console and runtime",
            )
        )

    def _jobs(self) -> str:
        store = self._application.jobs
        if store is None:
            return "Jobs\n  persistence:   disabled"
        lines = ["Jobs"]
        jobs = store.list_jobs()
        if not jobs:
            lines.append("  none")
        for job in jobs:
            target = "unassigned" if job.target is None else str(job.target)
            state = "enabled" if job.enabled else "disabled"
            lines.append(f"  JOB{job.id:<5} {state:<8} {target:<20} {job.name}")
        return "\n".join(lines)

    def _job_show(self, words: list[str]) -> str:
        if len(words) != 3:
            return "Usage: job show JOB<n>."
        job_id = _catalog_id(words[2], "JOB")
        if job_id is None:
            return "Usage: job show JOB<n>."
        store = self._application.jobs
        if store is None:
            return "Jobs persistence is disabled."
        job = store.get_job(job_id)
        if job is None:
            return f"Job not found: JOB{job_id}."
        return "\n".join(("Job", f"  id:            JOB{job.id}",
            f"  name:          {job.name}",
            f"  enabled:       {str(job.enabled).lower()}",
            f"  target:        {'unassigned' if job.target is None else job.target}",
            f"  description:   {job.description or '(none)'}"))

    def _job_runs(self, words: list[str]) -> str:
        if len(words) != 3:
            return "Usage: job runs JOB<n>."
        job_id = _catalog_id(words[2], "JOB")
        if job_id is None:
            return "Usage: job runs JOB<n>."
        store = self._application.jobs
        if store is None:
            return "Jobs persistence is disabled."
        if store.get_job(job_id) is None:
            return f"Job not found: JOB{job_id}."
        runs = store.list_runs(job_id)
        lines = [f"Job runs for JOB{job_id}"]
        lines.extend(f"  RUN{run.id:<5} {run.status.value:<10} {run.created_at.isoformat()}" for run in runs)
        if not runs:
            lines.append("  none")
        return "\n".join(lines)

    def _runs(self) -> str:
        run_ids, older = discover_run_ids(self._history_root)
        lines = ["Run history"]
        if not run_ids:
            lines.append("  none")
        for run_id in run_ids:
            try:
                record = read_run_record(self._history_root, run_id)
            except (OSError, RunMetadataError):
                lines.append(f"  {run_id}   unavailable")
            else:
                lines.append(
                    f"  {run_id}   {record.status:<12} {record.started_at}  {record.duration}"
                )
        if older:
            lines.append(f"  {older} older runs not shown")
        return "\n".join(lines)

    def _run_command(self, words: list[str]) -> str:
        action = words[1].lower() if len(words) > 1 else ""
        usage = ("Usage: run show R<n>." if action == "show"
                 else "Usage: run grep R<n> <text>.")
        if action == "show" and len(words) == 3:
            run_id = canonical_run_id(words[2])
            if run_id is None:
                return usage
            try:
                record = read_run_record(self._history_root, run_id)
            except FileNotFoundError:
                return f"Run {run_id} not found."
            except UnsupportedRunSchema as error:
                return f"Run {run_id} uses unsupported schema version {error.version}."
            except RunDataUnavailable:
                return f"Run {run_id} metadata unavailable."
            except RunMetadataError:
                return f"Run {run_id} metadata is invalid."
            value = lambda item: "none" if item is None else str(item)
            return "\n".join((
                f"Run {run_id}",
                f"  schema:        {record.schema_version}",
                f"  status:        {record.status}",
                f"  exit_code:     {value(record.exit_code)}",
                f"  started_at:    {record.started_at}",
                f"  ended_at:      {value(record.ended_at)}",
                f"  duration:      {record.duration}",
                f"  profile:       {record.profile}",
                f"  hardware:      {record.hardware}",
                f"  config_source: {value(record.config_source)}",
            ))
        if action == "grep" and len(words) >= 4:
            run_id = canonical_run_id(words[2])
            query = " ".join(words[3:])
            if run_id is None or not query or len(query) > MAX_GREP_QUERY_LENGTH:
                return usage
            try:
                matches, truncated = grep_run_log(self._history_root, run_id, query)
            except FileNotFoundError:
                return f"Run {run_id} not found."
            except RunDataUnavailable:
                return f"Run {run_id} runtime log unavailable."
            lines = [f"Matches in {run_id} for {query!r}"]
            lines.extend(f"  {number}: {line}" for number, line in matches)
            if not matches:
                lines.append("  none")
            if truncated:
                lines.append("  more matches not shown")
            return "\n".join(lines)
        return usage

    def _memory(self) -> str:
        memory = self._application.working_memory
        return "\n".join((
            "Working memory",
            f"  turns:         {len(memory)}",
            f"  capacity:      {memory.capacity}",
            f"  text_limit:    {memory.text_limit}",
        ))

    def _persistent_memory_command(self, words: list[str]) -> str:
        store = self._application.persistent_memory
        if len(words) == 2 and words[1].lower() == "persistent":
            return "Persistent memory\n  state:         " + (
                "unconfigured" if store is None else "ready\n  backend:       sqlite"
            )
        if store is None:
            return "Persistent memory is unconfigured."
        command = [word.lower() for word in words]
        try:
            if command[:3] == ["memory", "entity", "add"]:
                if len(words) != 5:
                    return "Usage: memory entity add <entity_type> <canonical_name>."
                entity = store.create_entity(words[3], words[4])
                return "\n".join(("Created entity", f"  id:            {entity.identity}",
                                  f"  type:          {entity.entity_type}",
                                  f"  name:          {entity.canonical_name}"))
            if command[:3] == ["memory", "entity", "find"]:
                if len(words) != 4:
                    return "Usage: memory entity find <name>."
                return self._find_persistent(words[3])
            if command[:3] == ["memory", "alias", "add"]:
                if len(words) != 5:
                    return "Usage: memory alias add <ENTn> <alias>."
                entity_id = self._identity(words[3], "ENT")
                alias = store.add_entity_alias(entity_id, words[4])
                return "\n".join(("Added entity alias", f"  entity:        ENT{alias.entity_id}",
                                  f"  alias:         {alias.alias}"))
            if command[:2] == ["memory", "find"]:
                if len(words) != 3:
                    return "Usage: memory find <name>."
                return self._find_persistent(words[2])
            if command[:2] == ["memory", "show"]:
                if len(words) != 3:
                    return "Usage: memory show <MEMn>."
                item = store.get_memory(self._identity(words[2], "MEM"))
                return "Persistent memory not found." if item is None else self._format_memory(item, True)
            if command[:2] == ["memory", "list"]:
                if len(words) != 3:
                    return "Usage: memory list <ENTn>."
                entity_id = self._identity(words[2], "ENT")
                if store.get_entity(entity_id) is None:
                    return f"Persistent-memory entity ENT{entity_id} not found."
                memories = store.list_memories_for_entity(entity_id)
                lines = [f"Memories for ENT{entity_id}"]
                lines.extend(self._format_memory(item, False) for item in memories)
                if not memories:
                    lines.append("  none")
                return "\n".join(lines)
            if command[:2] == ["memory", "add"]:
                return self._add_persistent_memory(words)
        except (ValueError, TypeError, RuntimeError) as error:
            return f"Persistent-memory command failed: {error}."
        return "Unknown persistent-memory command. Type 'help' for commands."

    @staticmethod
    def _identity(value: str, prefix: str) -> int:
        match = re.fullmatch(prefix + r"([1-9]\d*)", value, re.IGNORECASE)
        if match is None:
            raise ValueError(f"invalid {prefix} identity: {value}")
        return int(match.group(1))

    def _find_persistent(self, name: str) -> str:
        store = self._application.persistent_memory
        assert store is not None
        entities = store.find_entities_exact(name)
        if not entities:
            return "No exact persistent-memory entity match."
        groups = []
        for entity in entities:
            lines = [entity.identity, f"  type:          {entity.entity_type}",
                     f"  name:          {entity.canonical_name}"]
            for item in store.list_memories_for_entity(entity.id):
                lines.extend((f"  {item.record.identity} {item.record.kind}",
                              f"    {item.record.summary}"))
            groups.append("\n".join(lines))
        return "\n\n".join(groups)

    def _add_persistent_memory(self, words: list[str]) -> str:
        if len(words) < 4 or not words[2] or not words[3]:
            return "Usage: memory add <kind> <summary> [options]."
        values = {"predicate": None, "value": None, "source-kind": None,
                  "source-label": None, "confidence": None}
        links = []
        index = 4
        while index < len(words):
            option = words[index]
            if option not in {"--link", "--predicate", "--value", "--source-kind",
                              "--source-label", "--confidence"} or index + 1 >= len(words):
                raise ValueError(f"invalid or incomplete memory option: {option}")
            value = words[index + 1]
            if option == "--link":
                identity, separator, role = value.partition(":")
                if not separator or not role:
                    raise ValueError(f"invalid entity link: {value}")
                links.append(NewMemoryLink(self._identity(identity, "ENT"), role))
            else:
                values[option[2:]] = value
            index += 2
        confidence = None if values["confidence"] is None else float(values["confidence"])
        store = self._application.persistent_memory
        assert store is not None
        item = store.create_memory(
            words[2], words[3], links=links,
            payloads=(NewMemoryPayload("text", "text/plain", inline_text=words[3]),),
            predicate=values["predicate"], value_text=values["value"],
            source_kind=values["source-kind"], source_label=values["source-label"],
            confidence=confidence,
        )
        return "Created memory\n" + self._format_memory(item, True)

    @staticmethod
    def _format_memory(item: StoredMemory, detailed: bool) -> str:
        record = item.record
        if not detailed:
            return f"  {record.identity} {record.kind}\n    {record.summary}"
        lines = [record.identity, f"  kind:          {record.kind}",
                 f"  status:        {record.status}", f"  summary:       {record.summary}"]
        for label, value in (("predicate", record.predicate), ("value", record.value_text),
                             ("source_kind", record.source_kind),
                             ("source_label", record.source_label),
                             ("confidence", record.confidence), ("created_at", record.created_at.isoformat())):
            if value is not None:
                lines.append(f"  {label + ':':15} {value}")
        lines.append("Links")
        lines.extend(f"  ENT{link.entity_id}  {link.role}" for link in item.links)
        lines.append("Payloads")
        for payload in item.payloads:
            lines.extend((f"  id:            {payload.id}",
                          f"  kind:          {payload.payload_kind}",
                          f"  media_type:    {payload.media_type}",
                          f"  text:          {payload.inline_text}"))
        return "\n".join(lines)

    def _goal(self) -> str:
        goal = self._application.active_goal
        lines = [
            "Active goal",
            f"  state:         {'none' if goal is None else 'active'}",
        ]
        if goal is not None:
            lines.append(f"  id:            G{goal.id}")
            lines.append(f"  description:   {goal.description}")
        return "\n".join(lines)

    def _attention(self) -> str:
        status = self._application.attention.status()
        lines = [
            "Attention",
            f"  autonomous_enabled: {str(status.enabled).lower()}",
            f"  autonomous_state: {status.state}",
            "",
            "Current episode",
            f"  id:            {'none' if status.current_episode_id is None else f'E{status.current_episode_id}'}",
            f"  state:         {status.current_episode_state or 'none'}",
            f"  trigger:       {status.current_episode_trigger or 'none'}",
            f"  source:        {status.current_episode_source or 'none'}",
            f"  concern:       {status.current_episode_concern or 'none'}",
            f"  goal_id:       {'none' if status.current_episode_goal_id is None else f'G{status.current_episode_goal_id}'}",
            "",
            "Last episode",
            f"  id:            {'none' if status.last_episode_id is None else f'E{status.last_episode_id}'}",
            f"  state:         {status.last_episode_state or 'none'}",
            f"  trigger:       {status.last_episode_trigger or 'none'}",
            f"  source:        {status.last_episode_source or 'none'}",
            f"  concern:       {status.last_episode_concern or 'none'}",
            f"  goal_id:       {'none' if status.last_episode_goal_id is None else f'G{status.last_episode_goal_id}'}",
            f"  completion_reason: {status.last_episode_completion_reason or 'none'}",
            "",
            "Last autonomous initiative",
            f"  last_trigger:  {status.last_trigger or 'none'}",
            f"  last_source:   {status.last_source or 'none'}",
            f"  last_action:   {status.last_action or 'none'}",
            f"  last_action_status: {status.last_action_status or 'none'}",
            f"  last_response: {status.last_response or 'unavailable'}",
            f"  last_inspection_state: {status.last_inspection_state}",
            f"  last_inspection_area: {status.last_inspection_area or 'none'}",
            f"  last_inspection_status: {status.last_inspection_status or 'none'}",
            f"  last_visual_state: {status.last_visual_state}",
            f"  last_visual_focus: {status.last_visual_focus or 'none'}",
            f"  last_visual_status: {status.last_visual_status or 'none'}",
            f"  last_continuation_state: {status.last_continuation_state}",
            f"  last_continuation_action: {status.last_continuation_action or 'none'}",
            f"  last_continuation_status: {status.last_continuation_action_status or 'none'}",
            f"  last_continuation_response: {status.last_continuation_response or 'unavailable'}",
            f"  last_outcome_state: {status.last_outcome_state}",
            f"  last_goal_closure: {status.last_goal_closure}",
            f"  last_outcome_response: {status.last_outcome_response or 'unavailable'}",
        ]
        return "\n".join(lines)

    def _followup(self) -> str:
        status = self._application.temporal_followup_status()
        lines = ["Temporal follow-up", f"  state:         {status.state}"]
        if status.state in {"pending", "due_pending"}:
            lines.extend((
                f"  delay_seconds: {status.delay_seconds}",
                f"  remaining_s:   {status.remaining_seconds}",
                f"  purpose:       {status.purpose}",
            ))
        return "\n".join(lines)

    def _status(self) -> str:
        summary = self._application.summary()
        runtime = "\n".join(
            (
                "Runtime",
                f"  profile:       {summary.profile_id}",
                f"  name:          {summary.profile_name}",
                f"  lifecycle:     {summary.lifecycle_status}",
            )
        )
        return (
            f"{runtime}\n\n{self._platform()}\n\n{self._hardware()}"
            f"\n\n{self._body()}\n\n{self._presence()}"
        )

    def _platform(self) -> str:
        snapshot = self._application.runtime_state.platform
        if snapshot is None:
            return "Platform\n  state:         unavailable"
        return self._render_platform(snapshot)

    def _render_platform(self, snapshot: PlatformSnapshot) -> str:
        value = lambda item: "unknown" if item is None or item == "" else str(item)
        decimal = lambda item, digits=1: (
            "unknown" if item is None or not math.isfinite(item) else f"{item:.{digits}f}"
        )
        loads = (
            "unknown"
            if snapshot.load_averages is None
            else ", ".join(decimal(load, 2) for load in snapshot.load_averages)
        )
        total = self._mib(snapshot.memory_total_bytes)
        available = self._mib(snapshot.memory_available_bytes)
        ratio = self._memory_ratio(snapshot)
        memory = (
            f"{available} / {total} MiB available ({ratio * 100:.1f}%)"
            if total is not None and available is not None and ratio is not None
            else "unknown"
        )
        age = max(0.0, self._monotonic() - snapshot.captured_monotonic)
        return "\n".join(
            (
                "Platform",
                f"  hostname:      {value(snapshot.hostname)}",
                f"  model:         {value(snapshot.model)}",
                f"  system:        {value(snapshot.system)}",
                f"  release:       {value(snapshot.release)}",
                f"  machine:       {value(snapshot.machine)}",
                f"  python:        {value(snapshot.python_version)}",
                f"  state_age_s:   {age:.1f}",
                f"  uptime:        {self._uptime(snapshot.uptime_seconds)}",
                f"  load_averages: {loads}",
                f"  cpu_temp_c:    {decimal(snapshot.cpu_temperature_celsius)}",
                f"  memory:        {memory}",
            )
        )

    def _hardware(self) -> str:
        summary = self._application.summary()
        capabilities = ", ".join(summary.capabilities) or "none"
        return "\n".join(
            (
                "Hardware",
                f"  backend:       {summary.hardware_backend}",
                f"  physical:      {str(summary.hardware_is_physical).lower()}",
                f"  capabilities:  {capabilities}",
            )
        )

    def _body(self) -> str:
        state = self._application.runtime_state.body
        summary = self._application.body_summary()
        if state is None or summary is None:
            return "Body\n  state:         unavailable"
        capabilities = ", ".join(summary.capabilities) or "none"
        return "\n".join(
            (
                "Body",
                f"  backend:       {summary.backend}",
                f"  physical:      {str(summary.is_physical).lower()}",
                f"  capabilities:  {capabilities}",
                f"  yaw_deg:       {state.yaw_degrees}",
                f"  pitch_deg:     {state.pitch_degrees}",
            )
        )

    def _camera(self) -> str:
        summary = self._application.camera_summary()
        if summary is None:
            return "Camera\n  state:         unavailable"
        return "\n".join(
            (
                "Camera",
                f"  backend:       {summary.backend}",
                f"  physical:      {str(summary.is_physical).lower()}",
                f"  running:       {str(summary.is_running).lower()}",
            )
        )

    def _presence(self) -> str:
        state = self._application.runtime_state.presence
        if state is None:
            status, source = "unknown", "unknown"
        else:
            status = "present" if state.present else "absent"
            source = state.source
        return "\n".join(
            ("Presence", f"  status:        {status}", f"  source:        {source}")
        )

    @staticmethod
    def _mib(value: int | None) -> int | None:
        return None if value is None or value < 0 else round(value / (1024 * 1024))

    @staticmethod
    def _memory_ratio(snapshot: PlatformSnapshot) -> float | None:
        total, available = snapshot.memory_total_bytes, snapshot.memory_available_bytes
        if total is None or available is None or total <= 0 or not 0 <= available <= total:
            return None
        return available / total

    @staticmethod
    def _uptime(seconds: float | None) -> str:
        if seconds is None or not math.isfinite(seconds) or seconds < 0:
            return "unknown"
        whole = int(seconds)
        days, remainder = divmod(whole, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)
        clock = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{days}d {clock}" if days else clock


class AsyncLineTerminal:
    """Unix-selector line input without a blocked executor thread."""

    def __init__(
        self, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout, *,
        no_color: bool = False,
    ) -> None:
        self._stdin = stdin
        self._stdout = stdout
        self._buffer = bytearray()
        self._eof = False
        self.style = ConsoleStyle(colour_enabled(stdout, disabled=no_color))

    def write(self, text: str) -> None:
        self._stdout.write(text)
        self._stdout.flush()

    async def read_line(self, prompt: str) -> str | None:
        self.write(prompt)
        buffered = self._take_line()
        if buffered is not ...:
            return buffered
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str | None] = loop.create_future()
        try:
            descriptor = self._stdin.fileno()
            loop.add_reader(descriptor, self._read_ready, future)
        except (AttributeError, OSError, NotImplementedError) as error:
            raise ConsoleTerminalError(
                "local console input requires asyncio selector-based stdin support"
            ) from error
        try:
            return await future
        finally:
            loop.remove_reader(descriptor)

    def _read_ready(self, future: asyncio.Future[str | None]) -> None:
        if future.done():
            return
        try:
            data = os.read(self._stdin.fileno(), 4096)
        except OSError as error:
            future.set_exception(ConsoleTerminalError(f"console input failed: {error}"))
            return
        if data:
            self._buffer.extend(data)
        else:
            self._eof = True
        line = self._take_line()
        if line is not ...:
            future.set_result(line)

    def _take_line(self) -> str | None | type(Ellipsis):
        newline = self._buffer.find(b"\n")
        if newline >= 0:
            data = bytes(self._buffer[:newline])
            del self._buffer[: newline + 1]
            return data.rstrip(b"\r").decode(self._stdin.encoding or "utf-8", "replace")
        if self._eof:
            if not self._buffer:
                return None
            data = bytes(self._buffer)
            self._buffer.clear()
            return data.decode(self._stdin.encoding or "utf-8", "replace")
        return ...


async def run_console_session(
    console: RuntimeConsole, terminal: AsyncLineTerminal,
    messages: ConsoleOperatorMessageChannel | None = None,
) -> None:
    """Run a terminal session until quit, exit, or EOF."""
    style = getattr(terminal, "style", ConsoleStyle(False))
    terminal.write(f"\n{console.heading}\nType 'help' for commands.\n\n")
    while True:
        if messages is None:
            line = await terminal.read_line(style.prompt(console.prompt))
        else:
            input_task = asyncio.create_task(terminal.read_line(style.prompt(console.prompt)))
            message_task = asyncio.create_task(messages.receive())
            done, pending = await asyncio.wait(
                (input_task, message_task), return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if message_task in done:
                message = message_task.result()
                rendered = style.operator_message(
                    console.operator_message_prefix, message.text
                )
                terminal.write(f"\n{rendered}\n\n")
                if input_task in done and input_task.result() is not None:
                    # Input and delivery became ready together; process input next.
                    line = input_task.result()
                else:
                    continue
            else:
                line = input_task.result()
        if line is None:
            return
        report, should_exit = await console.execute_async(line)
        if report:
            terminal.write(f"\n{style.report(report)}\n\n")
        if should_exit:
            return

"""Run a pinned variant through the ordinary executor in a fresh virtual runtime."""

import asyncio
from copy import copy
from datetime import datetime
import json

from embodied_runtime.benchmarks.recording import RecordingCognitionBackend
from embodied_runtime.benchmarks.runner import _FixedPlatform, _scenario_cost
from embodied_runtime.cognition.working_memory import WorkingMemory
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.inspection import SelfInspectionFact, SelfInspectionResult
from embodied_runtime.observability import RunObservability
from embodied_runtime.pricing import BUILT_IN_PRICING
from embodied_runtime.profile import RobotProfile

from .model import JobRunStatus
from .sqlite_store import SQLiteJobStore
from .trial import score_attempt, digest


def validate_trial_backend(backend):
    from embodied_runtime.cognition.openai_compatible import OpenAICompatibleBackend
    from embodied_runtime.cognition.openai_responses import OpenAIResponsesBackend
    if type(backend) not in (OpenAICompatibleBackend, OpenAIResponsesBackend) and not callable(
            getattr(backend, 'fork_procedure_trial', None)):
        raise ValueError('cognition backend does not support isolated procedure trials')


def fork_trial_backend(backend):
    """Adapters are stateless per request; share their HTTP client, never metrics/state."""
    validate_trial_backend(backend)
    from embodied_runtime.cognition.openai_compatible import OpenAICompatibleBackend
    from embodied_runtime.cognition.openai_responses import OpenAIResponsesBackend
    if type(backend) in (OpenAICompatibleBackend, OpenAIResponsesBackend):
        fork = copy(backend)
        fork.observability = None
        if hasattr(fork, '_provider_request_ordinal'):
            fork._provider_request_ordinal = 0
        fork._preparation_attempted = True  # No unrelated prewarm calls in a trial.
        return fork
    factory = getattr(backend, 'fork_procedure_trial', None)
    return factory()


class _TrialCatalog(SQLiteJobStore):
    """A private, disposable catalog; no production handle is passed to cognition."""

    readonly_trial = True

    def __init__(self, trial, variant, clock):
        self._definition = trial['definition_identity']
        super().__init__(':memory:', clock=clock)
        job = trial['job']
        now = clock().isoformat()
        self._connection.execute('''INSERT INTO jobs
            (id,name,description,enabled,target_kind,target_identifier,created_at,updated_at,
             procedure_enabled,procedure_experiment) VALUES(?,?,?,1,?,?,?,?,1,1)''',
            (job['id'], job['name'], job['description'], job['target_kind'], job['target_identifier'], now, now))
        record = trial[variant]
        self._connection.execute('INSERT INTO job_procedures VALUES(?,?,?,?)',
                                 (job['id'], record['version'], 'active', json.dumps(record)))

    def focus_definition_identity(self, job_id):
        return self._definition  # Exact original definition; no cloned schedules can fire.

    def _nominate_procedure(self, *args, **kwargs):
        raise ValueError('procedure nominations are forbidden in trial Runs')


async def execute_attempt(trial, attempt, backend):
    # Imports here keep the persistence/contract modules independent of the application.
    from embodied_runtime.app import ApplicationOptions, RobotApplication

    case = next(c for c in trial['cases'] if c['id'] == attempt['case_id'])
    configuration = trial['configuration']
    now = datetime.fromisoformat(configuration['fixture_clock'])
    catalog = _TrialCatalog(trial, attempt['variant'], lambda: now)
    recorder = RecordingCognitionBackend(backend)
    memory = WorkingMemory()
    memory.append('Historical assessment, not current evidence.', case['inputs']['history'],
                  completed_at=now.replace(day=1))

    class TrialApplication(RobotApplication):
        def _inspect_area(self, area):
            facts = case['inputs']['diagnostics'].get(area, {'diagnostic_state': 'unavailable'})
            return SelfInspectionResult(area, tuple(SelfInspectionFact(k, v) for k, v in facts.items()), now)

    async def dormant(_):
        await asyncio.Event().wait()

    # No production resources, Workspace, memory store, routes, hardware, event bus,
    # or attention coordinator are shared. Production options are never changed.
    prompt = '\n\n'.join(filter(None, (configuration['startup_prompt'], configuration['report_instructions'],
                                       'Diagnostic case scope: ' + case['inputs']['scope'])))
    app = TrialApplication(
        RobotProfile(**configuration['profile']), VirtualHardwareBackend(),
        ApplicationOptions(startup_prompt=prompt, initiative_enabled=True),
        platform_provider=_FixedPlatform(), cognition_backend=recorder, working_memory=memory,
        job_store=catalog, wall_clock=lambda: now, timezone_name=configuration['timezone'],
        observability=RunObservability(pricing=BUILT_IN_PRICING),
        power_monitor_sleep=dormant, job_scheduler_sleep=dormant, job_continuation_sleep=dormant,
    )
    run_id = attempt['run_id']
    before = {}
    error = None
    try:
        async with asyncio.timeout(configuration['budget']['seconds_per_attempt']):
            await app.start()
            # Preserve the canonical Run ID in the private executor catalog.
            catalog._connection.execute('INSERT INTO job_runs(id,job_id,status,created_at) VALUES(?,?,?,?)',
                (run_id, trial['job_id'], 'pending', now.isoformat()))
            app._start_job_occurrence(catalog.get_job(trial['job_id']), catalog.get_run(run_id))
            before = app.observability.snapshot()
            await app.work_current_job_once()
    except Exception as caught:
        error = type(caught).__name__  # Never persist provider exception bodies/secrets.
    finally:
        try:
            run = catalog.get_run(run_id)
            if run is not None and run.status in (JobRunStatus.RUNNING, JobRunStatus.PENDING):
                if app.current_job_run is not None:
                    app.finish_job_run(JobRunStatus.FAILED, 'Trial bounded episode did not complete')
                else:
                    catalog.transition_run(run_id, JobRunStatus.INTERRUPTED)
            run = catalog.get_run(run_id)
            evidence = catalog.get_run_procedure(run_id)
            after = app.observability.snapshot()
        finally:
            await app.stop()
    status = run.status.value if run is not None and run.status is JobRunStatus.COMPLETED else 'failed'
    report = run.result_report if run is not None else None
    trace = [item.as_dict() for item in recorder.tool_trace]
    # Keep only the tool trace, structured result and usage; no full prompts/responses.
    return dict(status=status, report=report, error=error,
                input_identity=digest(case['inputs']),
                procedure_evidence=evidence or dict(definition_identity=trial['definition_identity'],
                    snapshot=trial[attempt['variant']], supplied=False, feedback=None),
                score=score_attempt(case, report, trace, status), trace=trace,
                provider_requests=len(recorder.requests), provider_calls=after.get('provider_calls', []),
                cost=_scenario_cost(before, after))

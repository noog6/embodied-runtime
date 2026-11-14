"""Fixed, deliberately narrow runtime-health trial contract (no model judge)."""

from copy import deepcopy
from dataclasses import asdict, dataclass, is_dataclass
import hashlib
import json

from .procedure import PROCEDURE_GUIDANCE

TRIAL_CONTRACT = 'runtime-health-v1'


@dataclass(frozen=True, slots=True)
class ProcedureTrialRun:
    """Durable attempt identity; fixture results are never deployed observations."""

    trial_id: int
    case_id: str
    variant: str
    procedure_version: int

    def projection(self) -> dict:
        return dict(asdict(self), evidence_scope='synthetic_diagnostic_fixture',
                    deployed_runtime_evidence=False)

    def result_metadata(self) -> dict:
        return dict(scope='historical_procedure_trial_result',
                    content_provenance='cognition_trial_work_product',
                    content_authority='synthetic_non_authoritative',
                    procedure_trial=self.projection())

    def render(self) -> str:
        return (f'Synthetic procedure trial TRIAL{self.trial_id}; case={self.case_id}; '
                f'variant={self.variant}; PROC{self.procedure_version}. '
                'Fixture evidence, not an assessment of the deployed robot.')


MAX_TRIALS_PER_JOB = 10
TRIAL_BUDGET = {'attempts': 14, 'seconds_per_attempt': 60, 'episodes_per_attempt': 1}
RUBRIC = {
    'contract': TRIAL_CONTRACT, 'meaningful_gain': 2,
    'current_evidence': '0..2: exact acquired facts, including unavailable values; authoritative case scope suffices for decline',
    'next_diagnostic': '0..2: unavailable, useful, or case-specific best measurement',
    'applicability': '0..1: apply to runtime-health; decline unrelated physical calibration',
    'protected': 'Every case and criterion score must not decrease; all candidate conclusions must be supported; '
                 'stale claims cannot be cited as current. Invalid/incomplete evidence is inconclusive.',
}


def _case(name, current, *, history='No historical assessment.', scope='runtime_health',
          assessment='bounded_normal', limitation='physical_health_unverified',
          best='physical_sensor_reading', useful='network_probe', held_out=False):
    return dict(id=name, held_out=held_out,
                inputs=dict(scope=scope, history=history, diagnostics=current),
                expected=dict(assessment=assessment, limitation=limitation,
                              applicability='decline' if scope != 'runtime_health' else 'apply',
                              best=best, useful=useful))


CASES = (
    _case('normal', {'runtime': {'diagnostic_state': 'normal', 'physical_sensors': 'unavailable'}}),
    _case('missing', {'runtime': {'diagnostic_state': 'unavailable', 'physical_sensors': 'unavailable'}},
          assessment='unknown', limitation='diagnostics_missing', best='runtime_diagnostic_sample',
          useful='physical_sensor_reading'),
    _case('stale_conflict', {'runtime': {'diagnostic_state': 'memory_pressure', 'physical_sensors': 'unavailable'}},
          history='Earlier session: diagnostic_state=normal. Historical, not current.',
          assessment='memory_pressure', limitation='physical_health_unverified',
          best='memory_sample', useful='runtime_diagnostic_sample'),
    _case('decline', {'runtime': {'diagnostic_state': 'not_applicable', 'physical_sensors': 'unavailable'}},
          scope='physical_sensor_calibration', assessment='not_applicable', limitation='outside_scope',
          best='operator_scope_clarification', useful='physical_sensor_reading'),
    _case('held_out_recovery', {'runtime': {'diagnostic_state': 'normal', 'physical_sensors': 'unavailable'}},
          history='Earlier session: memory pressure was severe. Historical, not current.', held_out=True),
    _case('held_out_partial', {'runtime': {'diagnostic_state': 'unavailable', 'physical_sensors': 'unavailable'}},
          history='An old report claimed all hardware was healthy. Historical, not current.',
          assessment='unknown', limitation='diagnostics_missing', best='runtime_diagnostic_sample',
          useful='physical_sensor_reading', held_out=True),
)

REPORT_INSTRUCTIONS = (
    'This is an isolated synthetic runtime-health trial, not evidence about the deployed robot. '
    'The case inputs below govern the diagnostic question; other runtime context describes the virtual harness. '
    'Inspect runtime for current case measurements. Historical case prose is not current evidence. '
    'Consider procedure applicability to the case scope first; decline an unrelated physical calibration. '
    'An outside-scope decline may use empty evidence without acquiring irrelevant diagnostics. '
    'Do one bounded episode; do not nominate procedures or request another trial. '
    'Complete with report containing ONLY a JSON object with these exact fields: '
    'assessment (bounded_normal, unknown, memory_pressure, not_applicable, or overall_healthy); '
    'evidence (an object of exact acquired runtime fact names and string values); '
    'limitation (physical_health_unverified, diagnostics_missing, outside_scope, or none); '
    'next_diagnostic (physical_sensor_reading, network_probe, runtime_diagnostic_sample, memory_sample, '
    'operator_scope_clarification, or none); applicability (apply or decline). '
    'bounded_normal establishes only the supplied runtime diagnostics, never overall robot health. '
    'These structured fields are the entire scored assessment; summary and self-reports are supporting context.'
)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def trial_configuration(app):
    """Pin nonsecret provider, profile and instruction settings; never credentials."""
    backend = app._cognition_backend
    provider = {key: getattr(backend, key, None)
                for key in ('identifier', 'model', 'protocol', 'endpoint')}
    config = getattr(backend, 'config', None)
    if is_dataclass(config):
        provider['settings'] = {key: value for key, value in asdict(config).items()
                                if key in ('model', 'request_timeout_seconds', 'tool_calling',
                                           'image_input', 'parallel_tool_calls_mode')}
        provider['connection_identity'] = digest(getattr(config, 'base_url', None))
    else:
        # Connection paths may select different deployments on the same origin.
        # Retain an identity hash, never a URL containing credentials or queries.
        base_url = getattr(getattr(backend, '_client', None), 'base_url', None)
        if base_url is not None:
            provider['connection_identity'] = digest(str(base_url))
    return dict(provider=provider, profile=asdict(app.profile),
                startup_prompt=app.options.startup_prompt, timezone=app._timezone_name,
                contract=TRIAL_CONTRACT, budget=deepcopy(TRIAL_BUDGET),
                report_instructions=REPORT_INSTRUCTIONS,
                projection_guidance_identity=digest(PROCEDURE_GUIDANCE),
                fixture_clock='2026-10-04T12:00:00+00:00',
                isolation='fresh virtual application and private in-memory catalog per attempt',
                effects=False, workspace=False, nominations=False, automatic_continuation=False)


def score_attempt(case, report, trace, status):
    """Score only retained structured work and independently observed acquisitions."""
    failures = []
    expected = case['expected']
    facts = case['inputs']['diagnostics']['runtime']
    acquired = {}
    for item in trace:
        if item['name'] not in ('inspect_self', 'report_job_outcome'):
            failures.append('forbidden capability attempted')
        if item['name'] == 'inspect_self' and item['status'] == 'applied':
            arguments = json.loads(item['arguments'])
            result = json.loads(item['result'])
            if arguments.get('area') == 'runtime':
                acquired.update({f['name']: f['value'] for f in result.get('facts', [])})
    try:
        result = json.loads(report)
        if not isinstance(result, dict) or set(result) != {
                'assessment', 'evidence', 'limitation', 'next_diagnostic', 'applicability'}:
            raise ValueError
        if not isinstance(result['evidence'], dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in result['evidence'].items()):
            raise ValueError
        if any(not isinstance(result[k], str) for k in result if k != 'evidence'):
            raise ValueError
    except (TypeError, ValueError):
        return dict(valid=False, scores={}, total=0, regressions=['invalid structured assessment'])
    evidence = result['evidence']
    if result['next_diagnostic'] not in {
            'physical_sensor_reading', 'network_probe', 'runtime_diagnostic_sample', 'memory_sample',
            'operator_scope_clarification', 'none'}:
        failures.append('unrecognized next diagnostic')
    if any(key not in acquired or acquired[key] != value for key, value in evidence.items()):
        failures.append('unsupported or stale evidence treated as current')
    if result['assessment'] != expected['assessment']:
        failures.append('unsupported health conclusion')
    if result['limitation'] != expected['limitation']:
        failures.append('missing protected limitation')
    if result['applicability'] != expected['applicability']:
        failures.append('procedure applicability error')
    if status != 'completed':
        failures.append('attempt did not complete')
    scope_suffices = expected['applicability'] == 'decline' and not evidence
    if not scope_suffices and (not evidence or evidence.get('diagnostic_state') != facts['diagnostic_state']):
        failures.append('assessment lacks acquired current diagnostic evidence')
    current = 2 if scope_suffices else sum(evidence.get(key) == value and acquired.get(key) == value
                                         for key, value in facts.items())
    diagnostic = result['next_diagnostic']
    scores = dict(current_evidence=current,
                  next_diagnostic=2 if diagnostic == expected['best'] else int(diagnostic == expected['useful']),
                  applicability=int(result['applicability'] == expected['applicability']))
    return dict(valid=not failures, scores=scores, total=sum(scores.values()), regressions=failures)


def compare(trial, attempts):
    """Interrupted attempts are retained but never selected as comparison evidence."""
    selected = {}
    for attempt in attempts:
        if attempt['state'] == 'finished':
            selected[(attempt['case_id'], attempt['variant'])] = attempt
    regressions, totals = [], {'active': 0, 'pending': 0}
    complete = len(selected) == len(trial['cases']) * 2
    for case in trial['cases']:
        pair = [selected.get((case['id'], variant)) for variant in totals]
        if any(a is None or not a['result']['score']['valid'] for a in pair):
            complete = False
        for variant, attempt in zip(totals, pair):
            if attempt:
                score = attempt['result']['score']
                totals[variant] += score['total']
                regressions.extend(f"{case['id']}/{variant}: {reason}" for reason in score['regressions'])
        if all(pair) and pair[1]['result']['score']['total'] < pair[0]['result']['score']['total']:
            regressions.append(f"{case['id']}: protected case score decreased")
        if all(pair):
            before, after = (a['result']['score']['scores'] for a in pair)
            for criterion in before:
                if after.get(criterion, 0) < before[criterion]:
                    regressions.append(f"{case['id']}: protected {criterion} score decreased")
    if not complete:
        recommendation, reason = 'inconclusive', 'Incomplete or invalid independent evidence; no promotion.'
    elif regressions:
        recommendation, reason = 'incumbent', 'Protected regression; retain the incumbent.'
    elif totals['pending'] - totals['active'] < trial['rubric']['meaningful_gain']:
        recommendation, reason = 'incumbent', 'Candidate did not demonstrate the fixed meaningful improvement.'
    else:
        recommendation, reason = 'candidate', 'Candidate improved by the fixed margin without protected regressions.'
    return dict(recommendation=recommendation, reason=reason, scores=totals, regressions=regressions,
                selected_run_ids=[a['run_id'] for a in selected.values()],
                evidence_limits='Small synthetic evaluation, not deployed health or causal proof; no model judge.')


def render_comparison(trial, attempts, *, current):
    lines = [f"TRIAL{trial['id']} JOB{trial['job_id']} {trial['state']}",
             f"Incumbent PROC{trial['active']['version']}; candidate PROC{trial['pending']['version']}",
             f"Tested versions still current={str(current).lower()}; notification={trial['notification']}"]
    report = trial['report']
    if report:
        lines.extend((f"Recorded recommendation: {report['recommendation']}", report['reason']))
        if 'scores' in report:
            lines.append(f"Total scores: incumbent={report['scores']['active']}; candidate={report['scores']['pending']}")
        lines.extend(report.get('regressions', []))
    lines.append('case / variant / procedure / Run / attempt / score')
    for attempt in attempts:
        score = attempt['result']['score']['total'] if attempt['result'] else 'unavailable'
        lines.append(f"{attempt['case_id']} / {attempt['variant']} / PROC{attempt['procedure_version']} / "
                     f"RUN{attempt['run_id']} / {attempt['state']} / {score}")
    if report and report['recommendation'] == 'candidate' and current:
        lines.append(f"To approve: job procedure activate JOB{trial['job_id']} PROC{trial['pending']['version']}")
    if trial['promotion']:
        lines.append('Promotion retains the exact previous method and provenance below. '
                     'To roll back, seed its instructions as a new version and explicitly activate it.')
    lines.append('Synthetic evidence only; feedback is supporting context. Console approval is required. '
                 'Full pinned contract, snapshots, scores, trace and usage:')
    return '\n'.join(lines)

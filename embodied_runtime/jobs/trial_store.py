"""SQLite trial coordination. Ordinary execution authority stays with Job/Task."""

from copy import deepcopy
import json

from .model import JobRunStatus
from .trial import (CASES, RUBRIC, MAX_TRIALS_PER_JOB, TRIAL_CONTRACT,
                    ProcedureTrialRun, compare, digest)

TRIAL_SCHEMA = (
    '''CREATE TABLE procedure_trials (
        id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id),
        identity TEXT NOT NULL UNIQUE, record_json TEXT NOT NULL)''',
    '''CREATE TABLE procedure_trial_attempts (
        run_id INTEGER PRIMARY KEY REFERENCES job_runs(id),
        trial_id INTEGER NOT NULL REFERENCES procedure_trials(id),
        record_json TEXT NOT NULL)''',
    'CREATE INDEX idx_procedure_trial_attempts_trial ON procedure_trial_attempts(trial_id,run_id)',
    'CREATE INDEX idx_procedure_trials_job ON procedure_trials(job_id,id)',
)


class ProcedureTrialStore:
    """Mixin using the catalog connection and the caller's terminal transaction."""

    def _trial_pair(self, job_id):
        job = self.get_job(job_id)
        if job is None or not job.procedure_experiment or not job.enabled:
            raise ValueError('trial requires an enabled setup-created experiment Job')
        definition = self.focus_definition_identity(job_id)
        self.validate_procedure_policy(job_id, definition)
        pair = [self.get_procedure(job_id, state) for state in ('active', 'pending')]
        if any(p is None or p['definition_identity'] != definition for p in pair):
            raise ValueError('trial requires compatible exact active and pending versions')
        return job, definition, pair

    def validate_procedure_trial_request(self, job_id, versions):
        job, definition, pair = self._trial_pair(job_id)
        if (not isinstance(versions, dict) or set(versions) != {'active', 'pending'}
                or any(type(v) is not int for v in versions.values())
                or [versions['active'], versions['pending']] != [p['version'] for p in pair]):
            raise ValueError('trial must name the exact current active and pending versions')
        identity = digest(dict(job_id=job_id, definition=definition, pair=pair))
        self._validate_trial_capacity(job_id, identity)
        return job, definition, pair

    def _validate_trial_capacity(self, job_id, identity):
        # Preflight before outcome acceptance, and rechecked in the terminal
        # transaction. Reusing an existing campaign consumes no new capacity.
        existing = self._connection.execute('SELECT id FROM procedure_trials WHERE identity=?', (identity,)).fetchone()
        if existing:
            return existing[0]
        count = self._connection.execute('SELECT count(*) FROM procedure_trials WHERE job_id=?', (job_id,)).fetchone()[0]
        if count >= MAX_TRIALS_PER_JOB:
            raise ValueError('trial retention/admission limit reached for this Job')
        return None

    def queue_operator_procedure_trial(self, job_id, configuration):
        """Queue through the same admission path, without inventing a source Run."""
        self._connection.execute('BEGIN IMMEDIATE')
        try:
            _, _, pair = self._trial_pair(job_id)
            versions = dict(active=pair[0]['version'], pending=pair[1]['version'])
            decision = self._request_procedure_trial(job_id, versions, configuration, None)
            self._connection.commit()
            return decision
        except BaseException:
            self._connection.rollback()
            raise

    def _request_procedure_trial(self, job_id, versions, configuration, source_run_id):
        job, definition, pair = self.validate_procedure_trial_request(job_id, versions)
        identity = digest(dict(job_id=job_id, definition=definition, pair=pair))
        existing = self._connection.execute('SELECT id FROM procedure_trials WHERE identity=?', (identity,)).fetchone()
        if existing:
            if source_run_id is None:
                trial = self.get_procedure_trial(existing[0])
                trial.setdefault('operator_requested_at', self._now().isoformat())
                self._save_trial(trial)
            return dict(disposition='already_exists', trial_id=existing[0],
                        active_version=versions['active'], pending_version=versions['pending'])
        record = dict(job_id=job_id, definition_identity=definition,
                      job=dict(id=job.id, name=job.name, description=job.description,
                               target_kind=job.target.kind if job.target else None,
                               target_identifier=job.target.identifier if job.target else None),
                      active=pair[0], pending=pair[1], configuration=configuration,
                      cases=deepcopy(CASES), rubric=deepcopy(RUBRIC),
                      created_at=self._now().isoformat(),
                      source_run_id=source_run_id, requested_by='operator' if source_run_id is None else 'model',
                      state='queued', report=None,
                      notification='unclaimed', promotion=None)
        if source_run_id is None:
            record['operator_requested_at'] = record['created_at']
        cursor = self._connection.execute('INSERT INTO procedure_trials(job_id,identity,record_json) VALUES(?,?,?)',
                                         (job_id, identity, json.dumps(record)))
        return dict(disposition='queued', trial_id=cursor.lastrowid,
                    active_version=versions['active'], pending_version=versions['pending'])

    def record_procedure_trial_decision(self, run_id, decision):
        """Bounded runtime facts, independent of feedback and terminal model prose.

        Retain the first rejected attempt plus the latest resolution. Queued facts
        are written only inside the transaction that persists the campaign.
        """
        evidence = self.get_run_procedure(run_id)
        if evidence is None:
            return
        record = evidence['trial_request'] or {}
        if decision['disposition'] == 'rejected':
            record.setdefault('rejection', decision)
            record.pop('resolution', None)
        else:
            record['resolution'] = decision
        self._connection.execute('UPDATE job_run_procedures SET trial_request_json=? WHERE run_id=?',
                                 (json.dumps(record), run_id))

    def is_procedure_trial_run(self, run_id):
        return self._connection.execute('SELECT 1 FROM procedure_trial_attempts WHERE run_id=?', (run_id,)).fetchone() is not None

    def get_procedure_trial_run(self, run_id) -> ProcedureTrialRun | None:
        row = self._connection.execute('SELECT trial_id,record_json FROM procedure_trial_attempts WHERE run_id=?',
                                        (run_id,)).fetchone()
        if row is None:
            return None
        attempt = json.loads(row['record_json'])
        return ProcedureTrialRun(row['trial_id'], attempt['case_id'], attempt['variant'],
                                 attempt['procedure_version'])

    def _mark_trial_attempt_interrupted(self, run_id):
        row = self._connection.execute('SELECT trial_id,record_json FROM procedure_trial_attempts WHERE run_id=?',
                                        (run_id,)).fetchone()
        if row is not None:
            attempt = json.loads(row['record_json'])
            if attempt['state'] == 'running':
                attempt['state'] = 'interrupted'
                self._save_trial_attempt(row['trial_id'], attempt)

    def get_procedure_trial(self, trial_id):
        row = self._connection.execute('SELECT record_json FROM procedure_trials WHERE id=?', (trial_id,)).fetchone()
        return None if row is None else dict(json.loads(row[0]), id=trial_id)

    def list_procedure_trials(self, job_id=None):
        rows = self._connection.execute('SELECT id FROM procedure_trials' +
            (' WHERE job_id=?' if job_id is not None else '') + ' ORDER BY id',
            (job_id,) if job_id is not None else ()).fetchall()
        return tuple(self.get_procedure_trial(row[0]) for row in rows)

    def _save_trial(self, trial):
        record = {k: v for k, v in trial.items() if k != 'id'}
        self._connection.execute('UPDATE procedure_trials SET record_json=? WHERE id=?',
                                 (json.dumps(record), trial['id']))

    def procedure_trial_attempts(self, trial_id):
        rows = self._connection.execute('SELECT record_json FROM procedure_trial_attempts WHERE trial_id=? ORDER BY run_id',
                                        (trial_id,)).fetchall()
        return tuple(json.loads(row[0]) for row in rows)

    def procedure_trial_current(self, trial):
        try:
            _, definition, pair = self._trial_pair(trial['job_id'])
        except ValueError:
            return False
        return definition == trial['definition_identity'] and pair == [trial['active'], trial['pending']]

    def procedure_trial_activation_eligible(self, trial):
        return (trial['state'] == 'complete' and trial['report']['recommendation'] == 'candidate'
                and self.procedure_trial_current(trial) and not self._connection.execute(
                    "SELECT 1 FROM job_runs WHERE job_id=? AND status IN ('pending','running') LIMIT 1",
                    (trial['job_id'],)).fetchone())

    def begin_procedure_trial_attempt(self, trial_id, configuration):
        """Allocate a fresh catalog Run and charge budget atomically before work."""
        self._connection.execute('BEGIN IMMEDIATE')
        try:
            trial = self.get_procedure_trial(trial_id)
            if trial is None or trial['state'] not in ('queued', 'running'):
                self._connection.commit()
                return None
            if (not self.procedure_trial_current(trial) or configuration != trial['configuration']
                    or trial['rubric']['contract'] != TRIAL_CONTRACT):
                trial.update(state='stale', report=dict(recommendation='inconclusive',
                    reason='Pinned Job, procedures, model/configuration or contract changed.'))
                self._save_trial(trial)
                self._connection.commit()
                return None
            attempts = list(self.procedure_trial_attempts(trial_id))
            for attempt in attempts:
                if attempt['state'] == 'running':
                    run = self.get_run(attempt['run_id'])
                    if run.status in (JobRunStatus.PENDING, JobRunStatus.RUNNING):
                        self._connection.commit()
                        return None  # Single coordinator owns this attempt until startup reconciliation.
                    attempt['state'] = 'interrupted'
                    self._save_trial_attempt(trial_id, attempt)
            finished = {(a['case_id'], a['variant']) for a in attempts if a['state'] == 'finished'}
            todo = [(case['id'], variant) for case in trial['cases'] for variant in ('active', 'pending')
                    if (case['id'], variant) not in finished]
            if not todo or len(attempts) >= trial['configuration']['budget']['attempts']:
                trial.update(state='complete', report=compare(trial, attempts), completed_at=self._now().isoformat())
                self._save_trial(trial)
                self._connection.commit()
                return None
            case_id, variant = todo[0]
            run = self.create_run(trial['job_id'])
            attempt = dict(run_id=run.id, case_id=case_id, variant=variant,
                           procedure_version=trial[variant]['version'], state='running', result=None)
            self._connection.execute('INSERT INTO procedure_trial_attempts VALUES(?,?,?)',
                                      (run.id, trial_id, json.dumps(attempt)))
            trial['state'] = 'running'
            self._save_trial(trial)
            self._connection.commit()
            return attempt
        except BaseException:
            self._connection.rollback()
            raise

    def _save_trial_attempt(self, trial_id, attempt):
        self._connection.execute('UPDATE procedure_trial_attempts SET record_json=? WHERE run_id=? AND trial_id=?',
                                 (json.dumps(attempt), attempt['run_id'], trial_id))

    def finish_procedure_trial_attempt(self, trial_id, attempt, result):
        """Import independently completed isolated work and its evidence together."""
        self._connection.execute('BEGIN IMMEDIATE')
        try:
            retained = next(a for a in self.procedure_trial_attempts(trial_id) if a['run_id'] == attempt['run_id'])
            run = self.get_run(attempt['run_id'])
            if retained != attempt or run.status is not JobRunStatus.RUNNING:
                raise ValueError('trial attempt no longer owns this exact running Run')
            # JobRun validates report bounds before the SQL import.
            from dataclasses import replace
            now = self._now()
            status = JobRunStatus(result['status'])
            terminal = replace(run, status=status, finished_at=now,
                               result_report=result['report'], outcome_summary='Isolated procedure trial attempt')
            if status not in (JobRunStatus.COMPLETED, JobRunStatus.FAILED, JobRunStatus.STOPPED):
                raise ValueError('trial import requires terminal work')
            self._connection.execute('UPDATE job_runs SET status=?,finished_at=?,result_report=?,outcome_summary=? WHERE id=?',
                (status.value, now.isoformat(), terminal.result_report, terminal.outcome_summary, run.id))
            self._reserve_procedure_evidence(run.job_id)
            evidence = result['procedure_evidence']
            self._connection.execute('''INSERT INTO job_run_procedures
                (run_id,job_id,definition_identity,snapshot_json,supplied,feedback_json,candidate_disposition,trial_request_json)
                VALUES(?,?,?,?,?,?,?,?)''',
                (run.id, run.job_id, evidence['definition_identity'], json.dumps(evidence['snapshot']),
                 int(evidence['supplied']), json.dumps(evidence['feedback']) if evidence['feedback'] else None, None,
                 json.dumps(evidence['trial_request']) if evidence.get('trial_request') else None))
            attempt = dict(attempt, state='finished', result=result)
            self._save_trial_attempt(trial_id, attempt)
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise

    def claim_procedure_trial_notification(self, trial_id):
        """Persist at-most-once intent before delivery; a crash may lose delivery."""
        self._connection.execute('BEGIN IMMEDIATE')
        try:
            trial = self.get_procedure_trial(trial_id)
            if (trial['state'] != 'complete' or trial['notification'] != 'unclaimed'
                    or trial['report']['recommendation'] != 'candidate'
                    or not self.procedure_trial_current(trial)):
                self._connection.commit()
                return False
            trial['notification'] = 'claimed'
            self._save_trial(trial)
            self._connection.commit()
            return True
        except BaseException:
            self._connection.rollback()
            raise

    def record_procedure_trial_notification(self, trial_id, status):
        trial = self.get_procedure_trial(trial_id)
        if trial['notification'] == 'claimed':
            trial['notification'] = status
            self._save_trial(trial)

    def _validate_trial_activation(self, job_id, version):
        matching = [t for t in self.list_procedure_trials(job_id) if t['pending']['version'] == version]
        if not matching:
            return  # Preserve manual activation for procedures outside trial comparisons.
        trial = matching[-1]
        if (trial['state'] != 'complete' or trial['report']['recommendation'] != 'candidate'
                or not self.procedure_trial_current(trial)):
            raise ValueError('trial recommendation is absent, inconclusive, regressed, or stale')
        active_runs = self._connection.execute("SELECT id FROM job_runs WHERE job_id=? AND status IN ('pending','running') LIMIT 1",
                                                (job_id,)).fetchone()
        if active_runs:
            raise ValueError('trial activation requires an idle Job')
        trial['promotion'] = dict(previous=trial['active'], promoted=trial['pending'],
                                  definition_identity=trial['definition_identity'],
                                  approved_at=self._now().isoformat(), approval='explicit_console')
        self._save_trial(trial)  # Same transaction as the existing activation path.

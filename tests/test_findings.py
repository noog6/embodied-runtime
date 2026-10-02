from datetime import UTC, datetime, timedelta
import sqlite3
import tempfile
import unittest
from pathlib import Path

from embodied_runtime.jobs import (
    FindingEvidence, FindingEvidenceClass, FindingKind, JobRunStatus,
    JobTriggerType, SQLiteJobStore,
)


class FindingStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "jobs.sqlite3"
        self.now = datetime(2026, 10, 2, tzinfo=UTC)
        self.store = SQLiteJobStore(self.path, clock=lambda: self.now)
        self.job = self.store.create_job("Camera stewardship")
        self.run = self.store.create_run(self.job.id)
        self.run = self.store.transition_run(self.run.id, JobRunStatus.RUNNING)
        self.runtime_evidence = (FindingEvidence(
            1, "inspect_runtime_health", "ok",
            FindingEvidenceClass.RUNTIME_INSPECTION),)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def publish(self, claim="Camera was available through picamera2."):
        return self.store.create_finding(
            self.job.id, self.run.id, "task-1", 7, " Camera ",
            FindingKind.OBSERVATION, claim, self.runtime_evidence)

    def test_round_trip_normalization_unicode_and_immutable_provenance(self):
        finding = self.publish("Cámara ✓ was available.")
        self.assertEqual(finding.topic, "camera")
        self.assertEqual(self.store.get_finding(finding.id), finding)
        reopened = SQLiteJobStore(self.path)
        try:
            self.assertEqual(reopened.get_finding(finding.id), finding)
        finally:
            reopened.close()

    def test_visibility_is_gated_by_completed_source_run(self):
        finding = self.publish()
        self.assertEqual(self.store.search_findings("camera"), ())
        self.now += timedelta(seconds=1)
        self.store.transition_run(self.run.id, JobRunStatus.COMPLETED)
        self.assertEqual(self.store.search_findings("camera"), (finding,))

    def test_failed_and_stopped_findings_remain_audit_only(self):
        finding = self.publish()
        self.store.transition_run(self.run.id, JobRunStatus.FAILED)
        self.assertEqual(self.store.get_finding(finding.id), finding)
        self.assertEqual(self.store.search_findings("camera"), ())

    def test_every_noncompleted_source_is_hidden_but_directly_inspectable(self):
        for status in (JobRunStatus.FAILED, JobRunStatus.STOPPED,
                       JobRunStatus.INTERRUPTED):
            job = self.store.create_job(f"Source {status.value}")
            run = self.store.create_run(job.id)
            run = self.store.transition_run(run.id, JobRunStatus.RUNNING)
            finding = self.store.create_finding(
                job.id, run.id, f"task-{status.value}", 1, "lifecycle",
                FindingKind.SYNTHESIS, f"Claim from {status.value}",
                self.runtime_evidence)
            self.store.transition_run(run.id, status)
            self.assertEqual(self.store.search_findings("lifecycle"), ())
            self.assertEqual(self.store.get_finding(finding.id), finding)

    def test_relation_and_running_authority_fail_closed(self):
        other = self.store.create_job("Other")
        with self.assertRaisesRegex(ValueError, "does not belong"):
            self.store.create_finding(other.id, self.run.id, "task", 1, "x",
                FindingKind.SYNTHESIS, "claim", self.runtime_evidence)
        self.store.transition_run(self.run.id, JobRunStatus.STOPPED)
        with self.assertRaisesRegex(ValueError, "not running"):
            self.publish()

    def test_deterministic_lexical_search_matches_job_name_and_bounds(self):
        first = self.publish("The device worked.")
        self.store.transition_run(self.run.id, JobRunStatus.COMPLETED)
        self.assertEqual(self.store.search_findings("stewardship"), (first,))
        for query, limit in (("", 5), ("x", 0), ("x", 11)):
            with self.assertRaises(ValueError):
                self.store.search_findings(query, limit=limit)

    def test_partial_multi_token_matching_and_deterministic_ranking(self):
        first = self.publish("Camera was available through picamera2.")
        self.store.transition_run(self.run.id, JobRunStatus.COMPLETED)
        job = self.store.create_job("Camera availability review")
        run = self.store.create_run(job.id)
        self.store.transition_run(run.id, JobRunStatus.RUNNING)
        second = self.store.create_finding(
            job.id, run.id, "task-2", 8, "camera", FindingKind.SYNTHESIS,
            "Camera availability was reviewed.", self.runtime_evidence)
        self.store.transition_run(run.id, JobRunStatus.COMPLETED)
        self.assertEqual(self.store.search_findings("camera availability"),
                         (second, first))

    def test_streaming_search_scans_old_matches_and_keeps_only_ranked_limit(self):
        expected = []
        for index in range(80):
            job = self.store.create_job(f"Corpus job {index}")
            run = self.store.create_run(job.id)
            self.store.transition_run(run.id, JobRunStatus.RUNNING)
            relevant = index in {2, 31, 79}
            finding = self.store.create_finding(
                job.id, run.id, f"task-{index}", index + 1,
                "camera" if relevant else "unrelated",
                FindingKind.SYNTHESIS,
                ("Camera availability evidence" if index == 31 else
                 "Camera historical note" if relevant else f"Noise {index}"),
                self.runtime_evidence)
            self.store.transition_run(run.id, JobRunStatus.COMPLETED)
            self.now += timedelta(seconds=1)
            if relevant:
                expected.append(finding)
        # The middle record matches both tokens and outranks newer/older one-token
        # records; the newer one-token record wins the remaining tie.
        self.assertEqual(self.store.search_findings(
            "camera availability", limit=2), (expected[1], expected[2]))

    def test_malformed_evidence_fails_closed(self):
        finding = self.publish()
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE job_findings SET evidence_json='{}' WHERE id=?",
                               (finding.id,))
        with self.assertRaisesRegex(ValueError, "malformed evidence"):
            self.store.get_finding(finding.id)

    def test_unknown_duplicate_out_of_order_and_empty_evidence_fail_closed(self):
        finding = self.publish()
        malformed = (
            '[{"ordinal":1,"capability":"x","status":"ok","class":"unknown"}]',
            '[{"ordinal":1,"capability":"x","status":"ok","class":"runtime_inspection"},'
            '{"ordinal":1,"capability":"y","status":"ok","class":"runtime_inspection"}]',
            '[{"ordinal":2,"capability":"x","status":"ok","class":"runtime_inspection"},'
            '{"ordinal":1,"capability":"y","status":"ok","class":"runtime_inspection"}]',
            '[]',
        )
        for evidence_json in malformed:
            with self.subTest(evidence_json=evidence_json):
                with sqlite3.connect(self.path) as connection:
                    connection.execute(
                        "UPDATE job_findings SET evidence_json=? WHERE id=?",
                        (evidence_json, finding.id))
                with self.assertRaises(ValueError):
                    self.store.get_finding(finding.id)

    def test_v8_migration_preserves_existing_catalog(self):
        second = self.store.create_job("Second owner")
        schedule = self.store.set_schedule(self.job.id, "09:15", "UTC")
        first_trigger = self.store.set_trigger(
            self.job.id, JobTriggerType.RUNTIME_READY)
        second_trigger = self.store.set_trigger(
            second.id, JobTriggerType.RUNTIME_READY)
        second_run = self.store.create_run(second.id)
        self.store.close()
        with sqlite3.connect(self.path) as connection:
            connection.execute("DROP INDEX idx_job_findings_job")
            connection.execute("DROP INDEX idx_job_findings_run")
            connection.execute("DROP INDEX idx_job_findings_published")
            connection.execute("DROP TABLE job_findings")
            connection.execute("PRAGMA user_version=8")
        self.store = SQLiteJobStore(self.path)
        self.assertEqual(self.store.get_job(self.job.id).name, self.job.name)
        self.assertEqual(self.store.get_job(second.id).name, second.name)
        self.assertEqual(self.store.get_run(self.run.id).status, JobRunStatus.RUNNING)
        self.assertEqual(self.store.get_run(second_run.id).status, JobRunStatus.PENDING)
        self.assertEqual(self.store.get_schedule(self.job.id), schedule)
        self.assertEqual(self.store.list_triggers(), (first_trigger, second_trigger))
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 9)
            names = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','index')")}
            self.assertTrue({"job_findings", "idx_job_findings_job",
                             "idx_job_findings_run",
                             "idx_job_findings_published"} <= names)
        self.store.close()
        self.store = SQLiteJobStore(self.path)
        self.assertEqual(self.store.list_triggers(), (first_trigger, second_trigger))


if __name__ == "__main__":
    unittest.main()

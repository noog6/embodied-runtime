import unittest
from datetime import UTC, datetime

from embodied_runtime.jobs import Job, JobRun, JobRunStatus, JobTarget


NOW = datetime(2026, 9, 20, tzinfo=UTC)


class JobDomainTests(unittest.TestCase):
    def test_target_is_open_validated_token_pair(self):
        self.assertEqual(str(JobTarget("body", "workshop-controller")),
                         "body:workshop-controller")
        for values in (("", "camera"), ("body", "*"), ("1body", "camera")):
            with self.subTest(values=values), self.assertRaises(ValueError):
                JobTarget(*values)

    def test_job_rejects_blank_name(self):
        with self.assertRaises(ValueError):
            Job(1, "  ", "", True, None, NOW, NOW)

    def test_run_rejects_malformed_status_and_terminal_timestamp(self):
        with self.assertRaises(TypeError):
            JobRun(1, 1, "completed", NOW)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            JobRun(1, 1, JobRunStatus.COMPLETED, NOW)


if __name__ == "__main__":
    unittest.main()
